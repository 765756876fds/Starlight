# -*- coding: utf-8 -*-
"""音乐播放并发缺口修复的确定性测试（第一刀）。

覆盖四个已确认并发缺口的修复行为：
1. play_online 过期请求（慢 URL 返回晚于新意图）不得覆盖新歌；
2. 切歌窗口内用户 pause 不得被 _play_song 的无条件 unpause 抹掉（起播即响）；
3. 模块 stop() 后自动跳过（坏文件/在线失败）不得再对 mpv 发起播放加载；
4. mpv 重启恢复不得覆盖重启窗口内用户发起的新选择。

全部使用 threading.Event 构造确定性时序，不依赖随机 sleep、不访问真实网络、
不启动真实 mpv 进程。线程在用例结束前 join 或由 _running 守卫自然退出。
"""
import threading
import time

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.core.event_bus import EventType
from app.music.controller import MusicController


class GatedFakeMpv:
    """不发真声音的 mpv 替身（协议满足 MpvAdapter）。

    loadfile_gate/loadfile_entered：可选确定性闸门——置 gate 时 loadfile 阻塞，
    entered 在 loadfile 进入时置位（用于"切歌持锁进行中"窗口的精确编排）。
    """

    def __init__(self):
        self.calls = []
        self._properties = {}
        self.volume = 70
        self.loadfile_gate = None
        self.loadfile_entered = None
        self.play_gate = None
        self.play_entered = None

    def on_property_change(self, name, cb):
        self.calls.append(("on_property_change", name))

    def start(self, taskkill=True, pipe_timeout=3.0, prop_timeout=0.5):
        self.calls.append(("start",))
        return True

    def stop(self):
        self.calls.append(("stop",))

    def stop_process(self):
        self.calls.append(("stop_process",))

    def is_running(self):
        return True

    def loadfile(self, filepath, mode="replace"):
        if self.loadfile_entered is not None:
            self.loadfile_entered.set()
        if self.loadfile_gate is not None:
            self.loadfile_gate.wait(5.0)
        self.calls.append(("loadfile", filepath))

    def play(self):
        if self.play_entered is not None:
            self.play_entered.set()
        if self.play_gate is not None:
            self.play_gate.wait(5.0)
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def toggle_pause(self):
        # 真实 mpv toggle 会翻转暂停态；Fake 同步翻转供断言
        self._properties["pause"] = not self._properties.get("pause", False)
        self.calls.append(("toggle_pause",))

    def seek(self, seconds, mode="relative"):
        self.calls.append(("seek", seconds, mode))

    def get_property(self, name, timeout=2.0):
        if name == "idle-active":
            return False
        if name == "playback-time":
            return 0.0
        if name == "duration":
            return 180.0
        if name == "pause":
            return bool(self._properties.get("pause", False))
        return self._properties.get(name)

    def set_property(self, name, value):
        self._properties[name] = value

    def get_live_playback_time(self):
        return 0.0

    def get_duration(self):
        return 180.0

    def get_current_time(self):
        return 0.0

    def get_volume(self):
        return self.volume

    def set_volume(self, volume):
        self.volume = volume

    def is_idle(self):
        return False

    def is_paused(self):
        return bool(self._properties.get("pause", False))

    def get_filename(self):
        return None


def _make_ctrl(tmp_path, fake, db_name="gaps.db"):
    cfg = {
        "database": str(tmp_path / db_name),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_gaps",
        "default_volume": 70,
    }
    with patch("app.music.controller.LxMusicClient") as lx_cls:
        # 测试环境强制落雪未连接：MagicMock 的 is_connected() 默认返回 truthy，
        # 会让 pause/next/toggle 等走"优先控制落雪"分支短路（与真实环境不符）。
        lx_cls.return_value.is_connected.return_value = False
        return MusicController(cfg, mpv_adapter=fake)


def _local_song(tmp_path, name, song_id):
    p = tmp_path / name
    p.write_bytes(b"fake-flac-bytes")
    return {"id": song_id, "title": name, "artist": "tester",
            "path": str(p), "duration": 10.0}


def _online_track(title="A", artist="B"):
    return MagicMock(title=title, artist=artist, source_name="kw",
                     track_id="t1", extra={}, duration_seconds=180)


class TestPlayOnlineStaleRequest:
    """问题 1：play_online 慢 URL 返回晚于新意图 → 锁内代次校验拒绝 loadfile。"""

    def test_stale_url_not_applied_after_new_play(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        gate = threading.Event()
        entered = threading.Event()

        def slow_get(track):
            entered.set()
            gate.wait(5.0)
            return {"url": "http://old.example/stale.mp3", "headers": None}

        manager.get_play_url.side_effect = slow_get
        ctrl.online_manager = manager
        try:
            t_old = threading.Thread(
                target=lambda: ctrl.play_online(_online_track("旧歌")),
                daemon=True)
            t_old.start()
            assert entered.wait(5.0), "旧请求应进入慢 get_play_url"
            # 用户随后发起新播放（本地歌）：_play_request_id 递增，成为当前有效意图
            new_song = _local_song(tmp_path, "new.flac", 2)
            ctrl.play_song(new_song)
            n_before = len(fake.calls)
            # 放行旧请求 → play_online 返回后必须锁内校验过期、拒绝 loadfile
            gate.set()
            t_old.join(timeout=5.0)
            assert not t_old.is_alive(), "play_online 线程应已结束"
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert new_loads == [], \
                f"旧 URL 不得 loadfile: {fake.calls[n_before:]}"
            assert any(c[0] == "loadfile" and c[1] == str(new_song["path"])
                       for c in fake.calls), "新本地歌应已 loadfile"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_current_url_still_applied(self, tmp_path):
        """回归：无并发新意图时，play_online 正常 loadfile（校验通过）。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        manager.get_play_url.return_value = {
            "url": "http://ok.example/now.mp3", "headers": None}
        ctrl.online_manager = manager
        try:
            ok = ctrl.play_online(_online_track("新歌"))
            assert ok is True
            assert any(c[0] == "loadfile" and c[1] == "http://ok.example/now.mp3"
                       for c in fake.calls), "当前 URL 应正常 loadfile"
            assert fake._properties.get("pause") is False, "播放后不应保持暂停"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestPauseDuringTrackSwitch:
    """问题 2：切歌（loadfile 持锁）进行中 pause 不被无条件 unpause 抹掉。"""

    def test_pause_during_switch_suspends_new_song(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "a.flac", 1)
            song_b = _local_song(tmp_path, "b.flac", 2)
            ctrl.queue.add_all([song_a, song_b])
            ctrl.queue.next()          # current = A
            ctrl._play_song(song_a)    # A 播放中（req=1）
            n_before = len(fake.calls)

            # 编排：next() 进入 B 的 loadfile（持锁中）时阻塞
            fake.loadfile_entered = threading.Event()
            fake.loadfile_gate = threading.Event()
            t_next = threading.Thread(target=ctrl.next, daemon=True)
            t_next.start()
            assert fake.loadfile_entered.wait(5.0), \
                "next 应进入 B 的 loadfile（持锁窗口）"

            # 切歌进行中用户 pause：修复后阻塞等锁，切歌完成后作用于新歌
            t_pause = threading.Thread(target=ctrl.pause, daemon=True)
            t_pause.start()
            time.sleep(0.05)  # 仅保证 pause 线程已进入等锁（不断言行为）
            # 放行 B 的 loadfile → next 完成（unpause）→ 释放锁 → pause 执行
            fake.loadfile_gate.set()
            t_next.join(timeout=5.0)
            t_pause.join(timeout=5.0)
            assert not t_next.is_alive(), "next 线程应已结束"
            assert not t_pause.is_alive(), "pause 线程应已结束"

            # 新歌 B 必须保持暂停（pause 意图未被 _play_song 的 unpause 抹掉）
            assert fake._properties.get("pause") is True, \
                f"切歌完成后 B 应处于暂停: {fake._properties}"
            idx_load = next(i for i, c in enumerate(fake.calls)
                            if c[0] == "loadfile" and c[1] == str(song_b["path"]))
            idx_pause = next(i for i, c in enumerate(fake.calls)
                             if c[0] == "pause")
            assert idx_pause > idx_load, \
                f"pause 必须发生在 B loadfile 之后: {fake.calls}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_resume_and_toggle_normal_regression(self, tmp_path):
        """回归：正常 resume / toggle 语义不变。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "a2.flac", 1)
            ctrl.queue.add_all([song_a])
            ctrl.queue.next()
            ctrl._play_song(song_a)
            # pause → resume
            ctrl.pause()
            assert fake._properties.get("pause") is True
            assert ctrl._paused_snapshot is True
            ctrl.resume()
            assert fake._properties.get("pause") is False
            assert ctrl._paused_snapshot is False
            # toggle：从播放态切到暂停
            ctrl.toggle()
            assert fake._properties.get("pause") is True
            ctrl.toggle()
            assert fake._properties.get("pause") is False
            # 最后一次 mpv 调用顺序：resume 后无异常
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestSkipAfterStop:
    """问题 3：模块停止后自动跳过不得再对 mpv 发起播放加载。"""

    def test_skip_bad_after_stop_does_not_load(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "sa.flac", 1)
            song_b = _local_song(tmp_path, "sb.flac", 2)
            ctrl.queue.add_all([song_a, song_b])
            ctrl.queue.next()
            ctrl._running = True   # 运行中（不 start，避免事件订阅污染全局总线）
            ctrl._play_song(song_a)
            n_before = len(fake.calls)
            ctrl.stop()  # _running = False
            # 模拟 stop 后 Timer 触发两条自动跳过路径
            ctrl._do_skip_bad(song_a)
            ctrl._do_skip_online(song_a)
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert new_loads == [], \
                f"stop 后自动跳过不得 loadfile: {fake.calls[n_before:]}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_skip_bad_still_works_when_running(self, tmp_path):
        """回归：_running=True 时坏文件跳过仍正常触发 next 播放下一首。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ra.flac", 1)
            song_b = _local_song(tmp_path, "rb.flac", 2)
            ctrl.queue.add_all([song_a, song_b])
            ctrl.queue.next()
            ctrl._running = True
            ctrl._play_song(song_a)
            n_before = len(fake.calls)
            ctrl._do_skip_bad(song_a)
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert len(new_loads) == 1 and new_loads[0][1] == str(song_b["path"]), \
                f"运行中坏文件跳过应播放下一首 B: {fake.calls[n_before:]}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_confirm_playback_skips_after_stop(self, tmp_path):
        """回归：stop 后 _confirm_playback 等待窗口不查询/操作 mpv。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ca.flac", 1)
            ctrl.queue.add_all([song_a])
            ctrl.queue.next()
            ctrl._running = True
            ctrl._play_song(song_a)
            n0 = len(fake.calls)
            ctrl.stop()
            ctrl._confirm_playback(song_a, song_a["path"])
            new_queries = [c for c in fake.calls[n0:] if c[0] == "get_property"]
            new_loads = [c for c in fake.calls[n0:] if c[0] == "loadfile"]
            assert new_queries == [] and new_loads == [], \
                f"stop 后 confirm 不得再查询/加载 mpv: {fake.calls[n0:]}"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestRestartResumeEpochGuard:
    """问题 4：重启恢复不得覆盖重启窗口内用户发起的新选择。"""

    def test_restart_resume_rejected_when_request_id_changed(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ea.flac", 1)
            song_b = _local_song(tmp_path, "eb.flac", 2)
            ctrl._running = True   # 运行中实例（重启恢复守卫需要）
            ctrl._play_song(song_a)   # req = 1，_current_song = A
            assert ctrl._play_request_id == 1
            ctrl._play_song(song_b)   # 重启窗口内用户切歌 → req = 2
            n_before = len(fake.calls)
            # 旧快照（req=1）恢复必须被拒绝：不得 loadfile 旧歌 A
            ctrl._try_resume_after_restart(song_a, 1)
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert new_loads == [], \
                f"重启旧快照不得覆盖用户新选择: {fake.calls[n_before:]}"
            assert ctrl._current_song is song_b or \
                ctrl._current_song.get("id") == 2, "当前歌曲应保持用户新选择"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_restart_resume_applies_when_request_id_unchanged(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "fa.flac", 1)
            ctrl._running = True
            ctrl._play_song(song_a)   # req = 1
            n_before = len(fake.calls)
            ctrl._try_resume_after_restart(song_a, 1)  # 代次一致 → 正常恢复
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert len(new_loads) == 1 and \
                new_loads[0][1] == str(song_a["path"]), \
                f"代次一致时重启应恢复旧歌: {fake.calls[n_before:]}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_restart_snapshot_captures_request_id(self, tmp_path):
        """_try_restart_mpv 快照必须携带重启时点代次并正确传给恢复逻辑。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ga.flac", 1)
            song_b = _local_song(tmp_path, "gb.flac", 2)
            ctrl._running = True
            ctrl._play_song(song_a)
            ctrl._play_song(song_b)   # 重启前当前歌 = B，req = 2
            ctrl._mpv_restart_count = 0
            ctrl._mpv_restart_window_ts = 0.0  # 窗口外 → 计数重置
            n_before = len(fake.calls)
            ctrl._try_restart_mpv("test-snapshot")
            # 等待重启 worker 收尾（pending 清除）
            t0 = time.time()
            while ctrl._mpv_restart_pending and time.time() - t0 < 3.0:
                time.sleep(0.02)
            assert ctrl._mpv_restart_pending is False, "重启 worker 应已收尾"
            # 快照代次 = 重启时点代次 2，当前代次未变 → 恢复 B（不是旧歌 A）
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert len(new_loads) == 1 and \
                new_loads[0][1] == str(song_b["path"]), \
                f"重启快照应恢复重启时点的歌曲 B: {fake.calls[n_before:]}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_restart_resume_blocked_when_user_plays_first(self, tmp_path):
        """P1-1 核心：用户新意图在线性化顺序上先于恢复操作 → 旧快照不得覆盖。

        构造"恢复操作等待业务锁"的真实窗口：用户 play_song(B) 持锁 loadfile
        进行中（gate 卡住）→ 重启恢复线程发起（等锁）→ 用户完成（req 递增）
        → 恢复获锁后校验失败，旧歌 A 绝不 loadfile。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ra1.flac", 1)
            song_b = _local_song(tmp_path, "rb1.flac", 2)
            ctrl._running = True
            ctrl._play_song(song_a)   # req=1，current=A
            # 用户 play_song(B) 持锁进行中（loadfile 被 gate 卡住）
            fake.loadfile_entered = threading.Event()
            fake.loadfile_gate = threading.Event()
            t_user = threading.Thread(
                target=lambda: ctrl.play_song(song_b), daemon=True)
            t_user.start()
            assert fake.loadfile_entered.wait(5.0), \
                "用户 play_song(B) 应进入 loadfile（持锁窗口）"
            # 重启恢复（旧快照 A, req=1）此刻发起 → 阻塞等锁
            t_resume = threading.Thread(
                target=lambda: ctrl._try_resume_after_restart(song_a, 1),
                daemon=True)
            t_resume.start()
            time.sleep(0.05)  # 仅保证恢复线程已进入等锁（不断言行为）
            n_before = len(fake.calls)
            fake.loadfile_gate.set()
            t_user.join(timeout=5.0)
            t_resume.join(timeout=5.0)
            assert not t_user.is_alive(), "用户播放线程应已结束"
            assert not t_resume.is_alive(), "恢复线程应已结束"
            # A 只能 loadfile 一次（最初 _play_song(song_a)）：旧快照不得再次重放；
            # B 必须 loadfile 一次（用户新歌）。放行后新增 loadfile 只能是 B 自己。
            a_loads = [c for c in fake.calls
                       if c[0] == "loadfile" and c[1] == str(song_a["path"])]
            b_loads = [c for c in fake.calls
                       if c[0] == "loadfile" and c[1] == str(song_b["path"])]
            assert len(a_loads) == 1, \
                f"旧快照 A 不得再次 loadfile（恢复覆盖用户新选择）: {fake.calls}"
            assert len(b_loads) == 1, f"用户新歌 B 应 loadfile: {fake.calls}"
            assert ctrl._current_song.get("id") == 2, \
                "最终播放目标应为用户新歌 B"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_restart_resume_skipped_after_stop(self, tmp_path):
        """P1-1 附加：模块已停止时重启恢复不得执行（worker 停止态保护）。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ra2.flac", 1)
            ctrl._running = True
            ctrl._play_song(song_a)   # req=1
            n_before = len(fake.calls)
            ctrl.stop()               # _running=False
            ctrl._try_resume_after_restart(song_a, 1)
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert new_loads == [], "stop 后重启恢复不得 loadfile"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestPlayPathConcurrency:
    """P1-2：play() 的无锁 _play_song 路径必须遵守暂停与切歌契约。"""

    def test_play_reload_does_not_overwrite_pause(self, tmp_path):
        """play() 重载当前歌曲分支：起播 loadfile 持锁进行中，pause 等锁后
        作用于新歌，不得被 _play_song 的无条件 unpause 抹掉。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "pa.flac", 1)
            ctrl.queue.add_all([song_a])
            ctrl.queue.next()          # current = A
            ctrl._running = True
            ctrl._play_song(song_a)    # A 已 loadfile（req=1）
            # fake.is_idle() → True：play() 走"没有文件，重新加载当前歌曲"分支
            fake.loadfile_entered = threading.Event()
            fake.loadfile_gate = threading.Event()
            t_play = threading.Thread(target=ctrl.play, daemon=True)
            t_play.start()
            assert fake.loadfile_entered.wait(5.0), \
                "play() 应进入重载 loadfile（持锁窗口）"
            # play() 持锁进行中 → 用户 pause 阻塞等锁
            t_pause = threading.Thread(target=ctrl.pause, daemon=True)
            t_pause.start()
            time.sleep(0.05)  # 仅保证 pause 线程已进入等锁（不断言行为）
            fake.loadfile_gate.set()
            t_play.join(timeout=5.0)
            t_pause.join(timeout=5.0)
            assert not t_play.is_alive(), "play 线程应已结束"
            assert not t_pause.is_alive(), "pause 线程应已结束"
            # play() 起播后用户暂停意图不得被抹掉
            assert fake._properties.get("pause") is True, \
                f"play() 起播后 pause 意图被抹掉: {fake._properties}"
            idx_load = next(i for i, c in enumerate(fake.calls)
                            if c[0] == "loadfile" and c[1] == str(song_a["path"]))
            idx_pause = next(i for i, c in enumerate(fake.calls)
                             if c[0] == "pause")
            assert idx_pause > idx_load, \
                f"pause 必须发生在 play() 的 loadfile 之后: {fake.calls}"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_play_random_with_empty_db_safe(self, tmp_path):
        """play() 随机播放分支冒烟：空库不崩、无播放副作用。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            ctrl._running = True
            ctrl.play()   # 队列空 + db 空 → get_random_songs([]) → 无副作用
            assert ctrl._current_song is None
            assert all(c[0] != "loadfile" for c in fake.calls), \
                f"空库 play 不得 loadfile: {fake.calls}"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestSearchFallbackEpoch:
    """P1-3：play_search_result 在线 fallback 的过期请求保护。"""

    def test_search_fallback_stale_url_not_applied(self, tmp_path):
        """慢 get_play_url 窗口内用户发起新播放 → 旧 URL 返回后锁内校验过期，
        不得覆盖新歌。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        gate = threading.Event()
        entered = threading.Event()
        manager.search_all.return_value = {"kw": [_online_track("旧歌")]}

        def slow_get(cand):
            entered.set()
            gate.wait(5.0)
            return {"url": "http://old.example/stale.mp3", "headers": None}

        manager.get_play_url.side_effect = slow_get
        ctrl.online_manager = manager
        try:
            t_old = threading.Thread(
                target=lambda: ctrl.play_search_result("旧歌", 0), daemon=True)
            t_old.start()
            assert entered.wait(5.0), "旧 fallback 应进入慢 get_play_url"
            # 用户随后播放本地新歌 B：req 递增，成为当前有效意图
            song_b = _local_song(tmp_path, "sb.flac", 2)
            ctrl.play_song(song_b)
            n_before = len(fake.calls)
            gate.set()
            t_old.join(timeout=5.0)
            assert not t_old.is_alive(), "fallback 线程应已结束"
            new_loads = [c for c in fake.calls[n_before:]
                         if c[0] == "loadfile"]
            assert new_loads == [], \
                f"旧 URL 不得 loadfile: {fake.calls[n_before:]}"
            assert any(c[0] == "loadfile" and c[1] == str(song_b["path"])
                       for c in fake.calls), "新本地歌应已 loadfile"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_search_fallback_current_url_applied(self, tmp_path):
        """回归：无并发新意图时，在线 fallback 正常 loadfile 当前 URL。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        manager.search_all.return_value = {"kw": [_online_track("新歌")]}
        manager.get_play_url.return_value = {
            "url": "http://ok.example/now.mp3", "headers": None}
        ctrl.online_manager = manager
        try:
            online_song = ctrl.play_search_result("新歌", 0)
            assert online_song is not None
            assert any(c[0] == "loadfile" and c[1] == "http://ok.example/now.mp3"
                       for c in fake.calls), "fallback 应正常 loadfile 当前 URL"
        finally:
            ctrl.stop()
            ctrl.db.close()


def _online_queue_song(title="A", artist="B"):
    """队列在线歌曲（source_type=online，带直取所需 track_id/source_name）。"""
    return {"id": 0, "title": title, "artist": artist,
            "source_type": "online", "source_name": "kw",
            "track_id": "t1", "path": "", "extra": {},
            "duration": 180.0}


def _spy_thread_factory():
    """返回 (Thread 子类, 已创建线程列表)：测试层记录 controller 创建的
    全部线程句柄，供确定性 join(timeout=...) 同步，不修改生产代码。"""
    created = []
    _orig = threading.Thread

    class _Spy(_orig):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            created.append(self)

    return _Spy, created


class TestOnlineWorkerStaleRequest:
    """第四刀-测试1：_resolve_online_worker（队列在线歌）过期 URL 不得覆盖新歌。

    覆盖直取分支（track_id/source_name 就绪 → manager.get_play_url 慢解析窗口）；
    重搜分支（_resolver.resolve）不在本轮覆盖，见交付报告。"""

    def test_worker_stale_url_not_applied_after_new_play(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        gate = threading.Event()
        entered = threading.Event()

        def slow_get(cand, quality=None):
            entered.set()
            gate.wait(5.0)
            return {"url": "http://old.example/a.mp3", "headers": None}

        manager.get_play_url.side_effect = slow_get
        ctrl.online_manager = manager
        ctrl._resolver = MagicMock()  # worker 入口要求 resolver 非 None（直取用不到）
        _Spy, created = _spy_thread_factory()
        try:
            online_song = _online_queue_song("A", "B")
            ctrl._running = True
            with patch("app.music.controller.threading.Thread", _Spy):
                # 队列在线歌 A 入队播放：_play_song → worker 进入慢 get_play_url
                ctrl._play_song(online_song)
                assert entered.wait(5.0), "worker 应进入慢 get_play_url"
                # 用户随后播放本地新歌 B：req 递增，成为当前有效意图
                song_b = _local_song(tmp_path, "wb.flac", 2)
                ctrl.play_song(song_b)
                assert any(c[0] == "loadfile" and c[1] == str(song_b["path"])
                           for c in fake.calls), "新本地歌 B 应已 loadfile"
                n_before = len(fake.calls)
                gate.set()
                # 确定性同步：持有 worker 线程句柄，有限 join 等待其真正完成，
                # 替代固定时长观察窗口（不依赖机器负载与调度）
                worker = next(t for t in created
                              if t.name == "music-online-resolve")
                worker.join(timeout=5.0)
                assert not worker.is_alive(), "worker 应已完成并退出"
                new_loads = [c for c in fake.calls[n_before:]
                             if c[0] == "loadfile"]
                assert new_loads == [], \
                    f"worker 过期 URL 不得 loadfile: {fake.calls[n_before:]}"
                assert ctrl._current_song.get("id") == 2, \
                    "当前歌曲应保持用户新歌 B（旧歌未回写）"
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_worker_current_url_applied(self, tmp_path):
        """回归：无并发时队列在线歌正常解析并 loadfile（确定性 join 同步）。"""
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        manager = MagicMock()
        manager.get_play_url.return_value = {
            "url": "http://ok.example/now.mp3", "headers": None}
        ctrl.online_manager = manager
        ctrl._resolver = MagicMock()
        _Spy, created = _spy_thread_factory()
        try:
            online_song = _online_queue_song("C", "D")
            ctrl._running = True
            with patch("app.music.controller.threading.Thread", _Spy):
                ctrl._play_song(online_song)
                worker = next(t for t in created
                              if t.name == "music-online-resolve")
                worker.join(timeout=5.0)
                assert not worker.is_alive(), "worker 应已完成并退出"
                assert any(c[0] == "loadfile" and c[1] == "http://ok.example/now.mp3"
                           for c in fake.calls), \
                    f"worker 应 loadfile 解析出的 URL: {fake.calls}"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestPlayRandomBranchConcurrency:
    """第四刀-测试2：play() 随机选歌分支（队列空 + db 有歌）不得抹掉并发 pause。"""

    def test_play_random_does_not_overwrite_pause(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        try:
            song_a = _local_song(tmp_path, "ra.flac", 1)
            # 确定性随机选歌：固定返回这一首（queue 空 → play() 走随机分支）
            ctrl.db.get_random_songs = MagicMock(return_value=[song_a])
            ctrl._running = True
            fake.loadfile_entered = threading.Event()
            fake.loadfile_gate = threading.Event()
            t_play = threading.Thread(target=ctrl.play, daemon=True)
            t_play.start()
            assert fake.loadfile_entered.wait(5.0), \
                "play() 应进入随机分支的 loadfile（持锁窗口）"
            t_pause = threading.Thread(target=ctrl.pause, daemon=True)
            t_pause.start()
            time.sleep(0.05)  # 仅保证 pause 线程已进入等锁（不断言行为）
            fake.loadfile_gate.set()
            t_play.join(timeout=5.0)
            t_pause.join(timeout=5.0)
            assert not t_play.is_alive(), "play 线程应已结束"
            assert not t_pause.is_alive(), "pause 线程应已结束"
            # 随机歌先进入播放流程，pause 随后生效；不得被起播 unpause 抹掉
            assert fake._properties.get("pause") is True, \
                f"play() 随机起播后 pause 意图被抹掉: {fake._properties}"
            idx_load = next(i for i, c in enumerate(fake.calls)
                            if c[0] == "loadfile" and c[1] == str(song_a["path"]))
            idx_pause = next(i for i, c in enumerate(fake.calls)
                             if c[0] == "pause")
            assert idx_pause > idx_load, \
                f"pause 必须发生在随机歌 loadfile 之后: {fake.calls}"
            assert ctrl._current_song.get("id") == 1, "随机歌应为当前歌曲"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestPlayResumePublishLock:
    """第四刀-测试3：play() 恢复分支（mpv.play + update_state + MUSIC_PLAY 发布）
    在业务锁内完成，并发控制操作不得插入中间窗口。"""

    def test_play_resume_holds_lock_through_publish(self, tmp_path):
        fake = GatedFakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        published = []

        def _on_music_play(e):
            published.append(1)

        ctrl.event_bus.subscribe(EventType.MUSIC_PLAY, _on_music_play)
        try:
            song_a = _local_song(tmp_path, "pa.flac", 1)
            ctrl.queue.add_all([song_a])
            ctrl.queue.next()
            ctrl._running = True
            ctrl._play_song(song_a)          # A 已 loadfile
            # 有文件且非 idle → play() 走恢复分支；门控 fake.play 建立持锁窗口
            fake.get_filename = lambda: str(song_a["path"])
            fake.play_entered = threading.Event()
            fake.play_gate = threading.Event()
            t_play = threading.Thread(target=ctrl.play, daemon=True)
            t_play.start()
            assert fake.play_entered.wait(5.0), \
                "play() 应进入恢复分支的 mpv.play()（持锁窗口）"
            # play 持锁卡在 mpv.play()：MUSIC_PLAY 尚未发布，pause 阻塞等锁
            t_pause = threading.Thread(target=ctrl.pause, daemon=True)
            t_pause.start()
            time.sleep(0.05)  # 仅保证 pause 线程已进入等锁（不断言行为）
            assert published == [], "MUSIC_PLAY 不得在 mpv.play() 之前发布"
            assert all(c[0] != "pause" for c in fake.calls), \
                f"publish 完成前 pause 不得插入执行: {fake.calls}"
            fake.play_gate.set()
            t_play.join(timeout=5.0)
            t_pause.join(timeout=5.0)
            assert not t_play.is_alive(), "play 线程应已结束"
            assert not t_pause.is_alive(), "pause 线程应已结束"
            # 顺序：mpv.play →（锁内）update_state + publish → 释放锁 → pause
            assert len(published) == 1, f"MUSIC_PLAY 应恰好发布一次: {published}"
            idx_resume = next(i for i, c in enumerate(fake.calls)
                              if c[0] == "play")
            idx_pause = next(i for i, c in enumerate(fake.calls)
                             if c[0] == "pause")
            assert idx_pause > idx_resume, \
                f"pause 必须发生在恢复分支之后: {fake.calls}"
            assert fake._properties.get("pause") is True, \
                f"恢复后 pause 意图应生效: {fake._properties}"
        finally:
            ctrl.event_bus.unsubscribe(EventType.MUSIC_PLAY, _on_music_play)
            ctrl.stop()
            ctrl.db.close()
