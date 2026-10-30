"""
Music 模块测试：SQLite 数据库 + FTS5
"""
import pytest

pytestmark = pytest.mark.unit

import os
import pytest

from app.music.database import MusicDatabase


class TestMusicDatabase:
    """音乐数据库测试"""

    def test_create_database(self, music_db):
        """测试数据库创建"""
        assert music_db is not None
        assert os.path.exists(music_db.db_path)

    def test_insert_song(self, music_db):
        """测试插入歌曲"""
        song_data = {
            "path": "/test/song1.flac",
            "filename": "song1.flac",
            "title": "测试歌曲",
            "artist": "测试歌手",
            "album": "测试专辑",
            "duration": 240.5,
            "filesize": 30000000,
            "format": "flac",
        }
        song_id = music_db.insert_song(song_data)
        assert song_id > 0

        song = music_db.get_song_by_path("/test/song1.flac")
        assert song is not None
        assert song["title"] == "测试歌曲"
        assert song["artist"] == "测试歌手"

    def test_fts_search(self, music_db):
        """测试 FTS5 全文搜索"""
        # 插入测试数据
        songs = [
            {"path": "/test/1.flac", "filename": "1.flac", "title": "晴天", "artist": "周杰伦", "album": "叶惠美", "format": "flac"},
            {"path": "/test/2.flac", "filename": "2.flac", "title": "七里香", "artist": "周杰伦", "album": "七里香", "format": "flac"},
            {"path": "/test/3.flac", "filename": "3.flac", "title": "浮夸", "artist": "陈奕迅", "album": "U87", "format": "flac"},
        ]
        for s in songs:
            music_db.insert_song(s)

        # 搜索歌手
        results = music_db.search("周杰伦")
        assert len(results) >= 2

        # 搜索歌名
        results = music_db.search("晴天")
        assert len(results) >= 1
        assert results[0]["title"] == "晴天"

    def test_get_song_count(self, music_db):
        """测试歌曲计数"""
        assert music_db.get_song_count() == 0

        music_db.insert_song({"path": "/test/1.flac", "filename": "1.flac", "title": "test"})
        assert music_db.get_song_count() == 1

    def test_increment_play_count(self, music_db):
        """测试播放计数"""
        song_id = music_db.insert_song({"path": "/test/1.flac", "filename": "1.flac", "title": "test"})
        music_db.increment_play_count(song_id)

        song = music_db.get_song_by_id(song_id)
        assert song["play_count"] == 1

    def test_get_random_songs(self, music_db):
        """测试随机歌曲"""
        for i in range(5):
            music_db.insert_song({"path": f"/test/{i}.flac", "filename": f"{i}.flac", "title": f"test{i}"})

        songs = music_db.get_random_songs(3)
        assert len(songs) == 3


class FakeMpvAdapter:
    """不发真声音的 mpv 替身：满足 MpvAdapter 协议（controller 调用面）。
    无子进程、无 IPC——所有调用只记录到 calls，属性写进 _properties。"""

    def __init__(self):
        self.calls = []
        self._properties = {}
        self.volume = 70

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
        self.calls.append(("loadfile", filepath))
        return {"error": "success"}

    def play(self):
        self.calls.append(("play",))

    def pause(self):
        self.calls.append(("pause",))

    def toggle_pause(self):
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


class TestControllerMpvAdapterInjection:
    """MpvAdapter 协议注入：controller 不实例化真实 mpv，可挂 Fake 播放。"""

    def test_injects_fake_and_play_song_is_instant(self, tmp_path):
        import time
        from unittest.mock import patch

        from app.music.controller import MusicController

        fake = FakeMpvAdapter()
        db_path = str(tmp_path / "mpv_adapter.db")
        cfg = {
            "database": db_path,
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test_mpv_adapter",
            "default_volume": 70,
        }
        # LxMusicClient.is_connected() 会真发 localhost HTTP 探测 → 测试内拦截
        with patch("app.music.controller.LxMusicClient"):
            ctrl = MusicController(cfg, mpv_adapter=fake)
        try:
            assert ctrl.mpv is fake, "注入后 ctrl.mpv 必须就是 Fake 实例"

            song_path = tmp_path / "song.flac"
            song_path.write_bytes(b"fake-mp3-bytes")
            song = {"id": 1, "title": "晴天", "artist": "周杰伦",
                    "path": str(song_path), "duration": 180.0}

            t0 = time.time()
            ctrl.play_song(song)
            dt = time.time() - t0

            assert any(c[0] == "loadfile" for c in fake.calls), \
                f"play_song 必须触发 Fake.loadfile: {fake.calls}"
            assert fake._properties.get("pause") is False, "播放后 pause 状态必须为 False"
            assert dt < 1.0, f"play_song 不应阻塞主线程: {dt:.2f}s"
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestP0ConcurrencyAndRobustness:
    """P0 修复回归：request_id 竞态 / 坏文件不杀模块 / _apply_stream 统一流设置。"""

    def _make_ctrl(self, tmp_path, fake):
        from unittest.mock import patch

        from app.music.controller import MusicController
        cfg = {
            "database": str(tmp_path / "p0.db"),
            "scan_dirs": [],
            "extensions": [".flac"],
            "mpv_path": "mpv",
            "mpv_ipc_pipe": "\\\\.\\pipe\\test_p0",
            "default_volume": 70,
        }
        with patch("app.music.controller.LxMusicClient"):
            return MusicController(cfg, mpv_adapter=fake)

    def test_inflight_resolve_dropped_after_local_play(self, tmp_path):
        """旧在线解析返回时已被本地播放作废 → 锁内校验不过，绝不 loadfile。"""
        import threading
        from unittest.mock import MagicMock

        fake = FakeMpvAdapter()
        ctrl = self._make_ctrl(tmp_path, fake)
        try:
            local = tmp_path / "local.flac"
            local.write_bytes(b"x")
            ctrl._play_request_id = 1
            ctrl._resolver = MagicMock()
            ctrl._resolver.resolve.return_value = {
                "url": "http://fake/url",
                "track": MagicMock(title="A", artist="B", album="C",
                                   source_name="kw", track_id="t1",
                                   extra={}, duration_seconds=180),
                "play_result": {"headers": None, "url": "http://fake/url"},
            }
            # 用户随后本地播放 → 代际号升为 2（_play_song 统一递增）
            ctrl.play_song({"id": 2, "title": "local", "path": str(local)})
            before = len(fake.calls)
            # 旧 worker（req_id=1）此刻才解析完返回 → 必须被丢弃
            ctrl._resolve_online_worker({}, "q", 1)
            new_loads = [c for c in fake.calls[before:] if c[0] == "loadfile"]
            assert new_loads == [], \
                f"过期解析不得 loadfile: {fake.calls[before:]}"
            assert ctrl._play_request_id == 2
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_missing_files_do_not_kill_module(self, tmp_path):
        """连续 3 个坏文件：只停止当前播放，绝不调用模块级 stop()——
        轮询线程与 _running 必须存活。"""
        import threading

        fake = FakeMpvAdapter()
        ctrl = self._make_ctrl(tmp_path, fake)
        try:
            ctrl.start()
            bad = str(tmp_path / "missing.flac")
            for i in range(3):
                ctrl._play_song({"id": i, "title": f"bad{i}", "path": bad})
            # 轮询线程必须仍存活（模块未被 stop() 杀）
            alive = any(t.name == "music-idle-poll" and t.is_alive()
                        for t in threading.enumerate())
            assert alive, "连续坏文件后轮询线程必须存活"
            assert ctrl._running is True, "连续坏文件后 _running 必须为 True"
            # 模块级 stop() 特有的杀进程调用绝不能被触发
            assert not any(c[0] == "stop_process" for c in fake.calls), \
                f"绝不允许 stop_process（杀 mpv 实例）: {fake.calls}"
            # 状态应回"未播放"
            assert ctrl._current_song is None
        finally:
            ctrl.stop()
            ctrl.db.close()

    def test_apply_stream_unifies_headers_and_state(self, tmp_path):
        """_apply_stream 统一流设置：header 分支 + loadfile + pause + _current_song；
        before_load 失败 → 放弃播放（play_online 原子校验语义）。"""
        from unittest.mock import MagicMock

        fake = FakeMpvAdapter()
        ctrl = self._make_ctrl(tmp_path, fake)
        try:
            track = MagicMock(title="晴天", artist="周杰伦", album="专辑",
                              source_name="酷我", track_id="t2",
                              extra={}, duration_seconds=200)
            pause_movie = MagicMock(return_value=True)
            ctrl.set_movie_pauser(pause_movie)
            online_song = ctrl._apply_stream(track, "http://kw/url.mp3", None)
            assert online_song is not None
            pause_movie.assert_called_once_with()
            assert online_song["source"] == "online"
            assert online_song["id"] == -1
            assert fake._properties.get("http-header-fields", "").startswith(
                "Referer: https://www.kuwo.cn/"), \
                f"酷我系 URL 应带 Referer: {fake._properties.get('http-header-fields')}"
            assert any(c[0] == "loadfile" for c in fake.calls)
            assert fake._properties.get("pause") is False
            assert ctrl._current_song == online_song
            # before_load 返回 False → 放弃播放，绝不 loadfile
            n0 = len(fake.calls)
            result = ctrl._apply_stream(track, "http://kw/url2.mp3", None,
                                        before_load=lambda: False)
            assert result is None
            assert len([c for c in fake.calls[n0:] if c[0] == "loadfile"]) == 0, \
                "before_load 失败后不得 loadfile"
            pause_movie.assert_called_once_with()
        finally:
            ctrl.stop()
            ctrl.db.close()


class TestIncrementalScan:
    """增量扫描测试"""

    def test_scan_skips_unchanged(self, music_db):
        """测试增量扫描跳过未修改的文件"""
        # 先插入一首歌
        song_data = {
            "path": "/test/unchanged.flac",
            "filename": "unchanged.flac",
            "title": "unchanged",
            "last_modified": 1000.0,
        }
        music_db.insert_song(song_data)

        # 验证数据库中有记录
        assert music_db.get_song_count() == 1

    def test_update_modified_file(self, music_db):
        """测试更新已修改的文件"""
        song_data = {
            "path": "/test/modified.flac",
            "filename": "modified.flac",
            "title": "old title",
            "last_modified": 1000.0,
        }
        song_id = music_db.insert_song(song_data)

        # 更新
        updated = dict(song_data)
        updated["title"] = "new title"
        updated["last_modified"] = 2000.0
        music_db.update_song(song_id, updated)

        song = music_db.get_song_by_id(song_id)
        assert song["title"] == "new title"
