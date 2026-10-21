"""音乐模块收口加固测试。

覆盖本轮修复：
1. MusicDatabase 并发锁 + delete_highlights 公开方法
2. scanner worker 走 db 公开 API（不再直接访问 db._conn）
3. jump_to_highlight 边界 clamp（不超歌曲时长）与 mpv 异常安全
4. 自动下一首 idle 轮询（mpv 事件回调不可达，独立轮询触发 auto_next）
"""
import pytest

pytestmark = pytest.mark.unit

import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.music.database import MusicDatabase


def _insert_song(db, path="/test/song.flac", title="Test"):
    return db.insert_song({
        "path": path,
        "filename": os.path.basename(path),
        "title": title,
        "duration": 200.0,
    })


class TestDatabaseLock:
    def test_delete_highlights_clears_all(self, tmp_path):
        db = MusicDatabase(str(tmp_path / "del.db"))
        sid = _insert_song(db)
        db.save_highlight(sid, 10.0, confidence=0.5, source="a")
        db.save_highlight(sid, 20.0, confidence=0.9, source="b")
        assert db.get_highlight(sid) is not None
        db.delete_highlights(sid)
        assert db.get_highlight(sid) is None
        db.close()

    def test_concurrent_writes_and_reads_safe(self, tmp_path):
        """多线程并发写/读同一连接不抛错（scanner worker + playback 场景）"""
        db = MusicDatabase(str(tmp_path / "conc.db"))
        sid = _insert_song(db)
        errors = []

        def worker(i):
            try:
                for k in range(15):
                    db.save_highlight(sid, 10.0 + i + k, confidence=0.5, source="t")
                    db.get_highlight(sid)
                    db.increment_play_count(sid)
            except Exception as e:  # pragma: no cover - 失败才会到这里
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        assert db.get_highlight(sid) is not None
        db.close()


class TestScannerHighlightWorker:
    def test_worker_uses_public_api_and_saves(self, tmp_path):
        """worker 计算后通过 delete_highlights+save_highlight 落库（不直接碰 _conn）"""
        song_path = str(tmp_path / "s.flac")
        db_path = str(tmp_path / "scan.db")
        db = MusicDatabase(db_path)
        db.insert_song({"path": song_path, "filename": "s.flac",
                        "title": "S", "duration": 100.0})

        from app.music.scanner import MusicScanner, get_source_tag
        sc = MusicScanner(db, [str(tmp_path)])
        with patch("app.music.scanner.detect_highlight", return_value=(30.0, 60.0, 0.8)):
            sc._highlight_worker()

        h = db.get_highlight(1)
        assert h is not None
        assert h["start_time"] == pytest.approx(30.0)
        assert h["source"] == get_source_tag()
        # 无结果时不应写入任何记录
        db.delete_highlights(1)
        with patch("app.music.scanner.detect_highlight", return_value=None):
            sc._highlight_worker()
        assert db.get_highlight(1) is None
        db.close()


class TestJumpToHighlightHardening:
    def _make_controller(self, tmp_path, highlight_data=None, duration=200.0):
        from app.music.controller import MusicController
        db_path = str(tmp_path / "ctrl.db")
        db = MusicDatabase(db_path)
        sid = _insert_song(db)
        if highlight_data:
            db.save_highlight(sid, **highlight_data)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        ctrl.mpv.get_duration.return_value = duration
        ctrl._current_song = {"id": sid, "title": "T", "duration": duration}
        return ctrl, db

    def test_jump_clamps_overlong_start(self, tmp_path):
        """highlight start 超过歌曲时长 → clamp 到 duration-1，不超界"""
        ctrl, db = self._make_controller(
            tmp_path, highlight_data={"start_time": 250.0, "end_time": 260.0,
                                      "confidence": 0.9, "source": "test"},
            duration=200.0)
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_once_with(pytest.approx(199.0), "absolute")
        db.close()

    def test_jump_clamps_negative_start(self, tmp_path):
        """highlight start 为负 → clamp 到 0"""
        ctrl, db = self._make_controller(
            tmp_path, highlight_data={"start_time": -5.0, "end_time": 10.0,
                                      "confidence": 0.9, "source": "test"},
            duration=200.0)
        ctrl.jump_to_highlight()
        ctrl.mpv.seek.assert_called_once_with(pytest.approx(0.0), "absolute")
        db.close()

    def test_jump_mpv_failure_safe(self, tmp_path):
        """mpv.seek 抛异常 → 不向调用方抛（intent router 不被炸）"""
        ctrl, db = self._make_controller(
            tmp_path, highlight_data={"start_time": 50.0, "end_time": 60.0,
                                      "confidence": 0.9, "source": "test"})
        ctrl.mpv.seek.side_effect = ConnectionError("mpv IPC 未连接")
        ctrl.jump_to_highlight()  # 不应抛异常
        db.close()

    def test_jump_mpv_duration_failure_safe(self, tmp_path):
        """get_duration 抛异常 → 降级为不 clamp，seek 仍被保护"""
        ctrl, db = self._make_controller(
            tmp_path, highlight_data={"start_time": 50.0, "end_time": 60.0,
                                      "confidence": 0.9, "source": "test"})
        ctrl.mpv.get_duration.side_effect = ConnectionError("mpv IPC 未连接")
        ctrl.mpv.seek.side_effect = ConnectionError("mpv IPC 未连接")
        ctrl.jump_to_highlight()  # 不应抛异常
        db.close()


class TestIdlePollAutoNext:
    def test_idle_rise_triggers_auto_next(self, tmp_path):
        """mpv idle 上升沿 → 自动切下一首（轮询实现）"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "idle.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        # idle 序列：前两轮 False（未播完），之后 True（播完触发 auto_next）。
        # 按属性名分发的 fake：idle-active 消费序列；playback-time/duration 返回固定值——
        # 新增的快照读取（_poll_idle 每轮多一次 playback-time）不会耗尽 side_effect 列表。
        idle_seq = [False, False] + [True] * 8
        idle_it = iter(idle_seq)

        def _fake_get_property(name, **kwargs):
            if name == "playback-time":
                return 5.0
            if name == "duration":
                return 10.0
            return next(idle_it)

        ctrl.mpv.get_property.side_effect = _fake_get_property
        # 注意：不再 mock is_idle()——_auto_next 守卫必须与轮询同源（实时 get_property），
        # 真实环境 is_idle() 读启动时缓存且从不更新（默认 False），不能作为切歌守卫。
        ctrl.mpv.is_paused.return_value = False
        ctrl.mpv.get_duration.return_value = 10.0

        s1 = str(tmp_path / "a.flac")
        s2 = str(tmp_path / "b.flac")
        open(s1, "w").close()
        open(s2, "w").close()
        ctrl.queue.add({"id": 1, "title": "A", "path": s1, "duration": 10.0})
        ctrl.queue.add({"id": 2, "title": "B", "path": s2, "duration": 10.0})
        ctrl.queue.play_at(0)

        ctrl.start()
        try:
            time.sleep(2.8)  # 轮询间隔 0.8s + 触发防抖 0.5s
            # idle 上升沿 → _auto_next → queue.next() → 播放第二首 → loadfile 被调用
            assert ctrl.queue.get_current_index() == 1, "应自动切到第二首"
            assert ctrl.mpv.loadfile.called
        finally:
            ctrl.stop()
        db.close()

    def test_auto_next_ignores_stale_is_idle_cache(self, tmp_path):
        """回归：is_idle() 读启动时缓存（事件死链下恒 False），
        _auto_next 守卫必须用实时 get_property 确认，否则切歌被陈旧缓存拦截。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "stale_idle.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        # 真实场景：mpv 事件回调死链 → is_idle() 返回启动时缓存 False（从不更新）
        ctrl.mpv.is_idle.return_value = False
        # 实时查询才是真相：已播完 → idle-active=True
        ctrl.mpv.get_property.return_value = True
        ctrl.mpv.is_paused.return_value = False
        ctrl.mpv.get_duration.return_value = 10.0

        s1 = str(tmp_path / "x.flac")
        s2 = str(tmp_path / "y.flac")
        open(s1, "w").close()
        open(s2, "w").close()
        ctrl.queue.add({"id": 1, "title": "X", "path": s1, "duration": 10.0})
        ctrl.queue.add({"id": 2, "title": "Y", "path": s2, "duration": 10.0})
        ctrl.queue.play_at(0)
        ctrl._running = True

        ctrl._auto_next()
        # 陈旧缓存 is_idle()=False 不应阻止切歌（守卫已改为实时 get_property）
        assert ctrl.queue.get_current_index() == 1, "陈旧 is_idle 缓存不应拦截切歌"
        assert ctrl.mpv.loadfile.called
        db.close()

    def test_start_idle_does_not_auto_play(self, tmp_path):
        """回归：冷启动 mpv 空闲（idle-active=True）不得自动播放。
        此前 _last_idle 初始硬编码 False，首次轮询把"启动空闲"误判为
        "播放结束"上升沿 → 启动即自动切歌播放。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "start_idle.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        ctrl.mpv.get_property.return_value = True  # mpv 冷启动即空闲
        s1 = str(tmp_path / "a.flac")
        open(s1, "w").close()
        ctrl.queue.add({"id": 1, "title": "A", "path": s1, "duration": 10.0})
        ctrl.queue.play_at(0)

        ctrl.start()
        try:
            time.sleep(2.5)  # 覆盖 2-3 轮轮询（0.8s/轮）
            ctrl.mpv.loadfile.assert_not_called(), "启动空闲不应自动播放"
        finally:
            ctrl.stop()
        db.close()

    def test_start_idle_baseline_read_failure_no_auto_play(self, tmp_path):
        """基线读取失败（IPC 未就绪）时保守取 True：同样不能自动播放。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "start_idle_fail.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        ctrl.mpv.get_property.side_effect = ConnectionError("IPC 未就绪")

        ctrl.start()
        try:
            time.sleep(2.5)
            assert ctrl._last_idle is True, "基线读取失败应保守取 True"
            ctrl.mpv.loadfile.assert_not_called()
        finally:
            ctrl.stop()
        db.close()

    def test_next_stays_idle_when_queue_at_end(self, tmp_path):
        """回归：顺序播放到队尾，next() 保持空闲，绝不跨界加载本地库。
        旧逻辑在队列空时自动 db.get_all_songs 重载整个本地库续播——在线歌单
        播完因此错误跳转到本地歌曲。新契约：队列尾 → mpv.stop() 回 idle、
        不 loadfile、无新 current_song（等待用户主动操作）。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "next_end.db")
        db = MusicDatabase(db_path)
        s1 = str(tmp_path / "a.flac")
        s2 = str(tmp_path / "b.flac")
        open(s1, "w").close()
        open(s2, "w").close()
        # 库里 2 首歌（旧逻辑 get_all_songs 重载来源，新逻辑不得触碰）
        db.insert_song({"path": s1, "filename": "a.flac", "title": "A",
                        "artist": "x", "album": "", "format": "flac"})
        db.insert_song({"path": s2, "filename": "b.flac", "title": "B",
                        "artist": "x", "album": "", "format": "flac"})
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        ctrl.mpv.is_paused.return_value = False
        ctrl.mpv.get_duration.return_value = 10.0
        # 队列 2 首，播放到末尾（B）
        ctrl.queue.add_all([{"id": 1, "title": "A", "path": s1, "duration": 10.0},
                            {"id": 2, "title": "B", "path": s2, "duration": 10.0}])
        ctrl.queue.play_at(1)
        ctrl._running = True

        ctrl.next()
        # 顺序到末尾 → 保持空闲：不 loadfile、无新歌、mpv 回 idle
        ctrl.mpv.loadfile.assert_not_called()
        assert ctrl.mpv.stop.called, "队列尾必须 mpv.stop() 回到 idle"
        assert ctrl._current_song is None, "不得自动载入本地库续播"
        db.close()

    def test_play_song_resets_idle_poll_state_on_bad_file(self, tmp_path):
        """回归：_play_song 必须重置 _last_idle。
        坏文件（MPEG header junk / 文件不存在）loadfile 后 mpv 仍/又回到 idle，
        若 _last_idle 停留在 True，轮询没有 False→True 上升沿 → 切歌永久卡死
        （表现为：切了歌但不播放，UI 卡住）。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "bad_file.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        # 上一首播完，轮询已把 _last_idle 置 True（坏文件场景入口状态）
        ctrl._last_idle = True
        s1 = str(tmp_path / "bad.flac")
        open(s1, "w").close()
        ctrl._play_song({"id": 1, "title": "BAD", "path": s1, "duration": 10.0})
        assert ctrl._last_idle is False, "尝试播放后必须重置轮询状态，使坏文件能再次触发切歌"
        # 文件不存在同样必须重置（否则坏路径卡死）
        ctrl._last_idle = True
        ctrl._play_song({"id": 2, "title": "GONE",
                         "path": str(tmp_path / "none.flac"), "duration": 10.0})
        assert ctrl._last_idle is False, "文件不存在时也必须重置轮询状态"
        db.close()

    def test_confirm_playback_skips_bad_file(self, tmp_path):
        """回归：坏文件 loadfile 后 mpv 仍 idle（解码失败）→ 确认线程自动跳过切下一首。
        否则 idle 轮询无上升沿，播放器永久卡住（名字切了但不播放）。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "bad_confirm.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        # 坏文件：loadfile 后 idle-active 仍 True（解码失败）
        ctrl.mpv.get_property.return_value = True
        ctrl.mpv.is_paused.return_value = False
        ctrl.mpv.get_duration.return_value = 10.0
        s1 = str(tmp_path / "bad.flac")
        s2 = str(tmp_path / "good.flac")
        open(s1, "w").close()
        open(s2, "w").close()
        ctrl.queue.add_all([{"id": 1, "title": "BAD", "path": s1, "duration": 10.0},
                            {"id": 2, "title": "GOOD", "path": s2, "duration": 10.0}])
        ctrl.queue.play_at(0)
        ctrl._running = True

        ctrl._play_song({"id": 1, "title": "BAD", "path": s1, "duration": 10.0})
        # 确认线程 + Timer 需约 1.3s；等待后应已自动切到下一首
        time.sleep(1.6)
        assert ctrl.queue.get_current_index() == 1, "坏文件应被自动跳过，切到下一首"
        db.close()

    def test_confirm_playback_keeps_good_file(self, tmp_path):
        """正常文件：loadfile 后 idle=False 且有播放时间 → 确认线程不干预。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "good_confirm.db")
        db = MusicDatabase(db_path)
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()
        # 正常播放：idle=False，playback-time=1.0，duration=10.0
        ctrl.mpv.get_property.side_effect = [False, 1.0, 10.0, False, 1.0, 10.0]
        ctrl.mpv.is_paused.return_value = False
        ctrl.mpv.get_duration.return_value = 10.0
        s1 = str(tmp_path / "ok.flac")
        open(s1, "w").close()
        ctrl.queue.add_all([{"id": 1, "title": "OK", "path": s1, "duration": 10.0}])
        ctrl.queue.play_at(0)
        ctrl._running = True

        ctrl._play_song({"id": 1, "title": "OK", "path": s1, "duration": 10.0})
        time.sleep(1.6)
        assert ctrl.queue.get_current_index() == 0, "正常文件不应被跳过"
        assert ctrl._skip_pending is False
        db.close()

    def test_cleanup_missing_songs(self, tmp_path):
        """清理失效歌曲：文件不存在的记录（连同 highlight）被删除，存在的保留。"""
        from app.music.controller import MusicController
        db_path = str(tmp_path / "cleanup.db")
        db = MusicDatabase(db_path)
        exist_file = str(tmp_path / "exist.flac")
        open(exist_file, "w").close()
        gone_file = str(tmp_path / "gone.flac")  # 不创建 → 路径失效
        id_exist = db.insert_song({"path": exist_file, "filename": "e.flac",
                                   "title": "EXIST", "artist": "x", "album": "",
                                   "format": "flac"})
        id_gone = db.insert_song({"path": gone_file, "filename": "g.flac",
                                  "title": "GONE", "artist": "x", "album": "",
                                  "format": "flac"})
        db.save_highlight(id_gone, 10.0, 20.0, 0.9, "manual")
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test",
            "default_volume": 70,
        }
        with patch("app.music.controller.MpvController"):
            ctrl = MusicController(cfg)
        ctrl.mpv = MagicMock()

        result = ctrl.cleanup_missing()
        assert result["deleted"] == 1, "应只删除 1 首失效歌"
        assert db.get_song_by_path(exist_file) is not None, "存在的歌必须保留"
        assert db.get_song_by_path(gone_file) is None, "失效歌必须删除"
        assert db.get_highlight(id_gone) is None, "失效歌的 highlight 应一并删除"
        assert db.get_song_count() == 1
        db.close()
