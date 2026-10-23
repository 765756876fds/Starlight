# -*- coding: utf-8 -*-
"""音乐控制器请求竞态验收测试（确定性构造，非随机压力）。

覆盖任务要求的四类场景：
A. 100 次快速播放请求 → 最后一个有效请求决定最终曲目；
   切换态不残留、fake MPV 记录与请求顺序一致。
B. 旧在线请求晚于新请求返回（resolver/取流阶段被 gate 阻塞，
   新请求完成后再释放）→ 旧请求被请求代次防线丢弃，不覆盖新状态。
C. 旧请求 loadfile 失败 + 新请求随后成功 → 旧请求异常清理
   不清掉新请求的切换态、当前曲目与播放状态。
D. 在线失败/空队列/延迟清理：由 test_music_loadfile_failure.py
   既有用例覆盖（本文件不重复）。

全部使用可控 Fake MPV 与 gate Event；不启动真实 mpv、不创建 UI。
"""
import threading

import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.core.state_store import StateNamespace
from app.music.controller import MusicController


class FakeMpv:
    """不发真声音的 mpv 替身（协议满足 MpvController 受测子集）。"""

    def __init__(self, idle_active=False):
        self.calls = []
        self._idle_active = idle_active
        self._properties = {}
        self.volume = 70

    def on_property_change(self, name, cb):
        self.calls.append(("on_property_change", name))

    def start(self, taskkill=True, pipe_timeout=3.0, prop_timeout=0.5):
        self.calls.append(("start",))
        return True

    def stop(self):
        self.calls.append(("stop",))
        return True

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
        self._properties["pause"] = not self._properties.get("pause", False)
        self.calls.append(("toggle_pause",))

    def seek(self, seconds, mode="relative"):
        self.calls.append(("seek", seconds, mode))

    def get_property(self, name, timeout=2.0):
        if name == "idle-active":
            return self._idle_active
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
        return self._idle_active

    def is_paused(self):
        return bool(self._properties.get("pause", False))

    def get_filename(self):
        return None


class FirstFailThenOkMpv(FakeMpv):
    """第一次 loadfile 抛异常且先递增代次（并发接管语义），此后正常成功。

    用于 C 场景：旧请求失败时"已有新请求递增代次"→ 旧清理不得清新状态；
    随后真实新请求成功 → 终态必须是新请求的完整成功态。
    """

    def __init__(self):
        super().__init__()
        self._ctrl = None
        self._first_done = False

    def bind(self, ctrl):
        self._ctrl = ctrl

    def loadfile(self, filepath, mode="replace"):
        self.calls.append(("loadfile", filepath))
        if not self._first_done:
            self._first_done = True
            self._ctrl._play_request_id += 1  # 并发切歌已递增代次
            raise ConnectionError("mpv IPC 未连接")
        return {"error": "success"}


def _make_ctrl(tmp_path, fake, db_name="request_race.db"):
    cfg = {
        "database": str(tmp_path / db_name),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_request_race",
        "default_volume": 70,
    }
    with patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        ctrl = MusicController(cfg, mpv_adapter=fake)
        ctrl.lx_connected = False
        return ctrl


def _local_song(tmp_path, song_id, title):
    p = tmp_path / f"{title}.flac"
    p.write_bytes(b"fake-flac-bytes")
    return {"id": song_id, "title": title, "path": str(p), "duration": 10.0}


def _playing_state(ctrl):
    return ctrl.state_store.get(StateNamespace.MUSIC, "playing", None)


def _online_song(title, track_id, url="http://stale.example/song.mp3"):
    return {"id": -1, "title": title, "artist": "Artist", "path": url,
            "duration": 10.0, "source_type": "online",
            "source_name": "demo", "track_id": track_id, "extra": {}}


class TestFastSequentialRequests:
    """A：100 次快速播放请求（play_song 直接链，无防抖干扰）。"""

    def test_100_play_requests_last_wins_and_state_consistent(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        songs = [_local_song(tmp_path, i, f"S{i}") for i in range(100)]

        for s in songs:
            ctrl.play_song(s)
            # 每个请求完成后切换态必须已复位（不残留）
            assert ctrl._is_switching_track is False

        # 最后一个有效请求决定最终曲目
        assert ctrl._current_song is songs[99]
        assert ctrl.queue.get_current() is songs[99]
        assert _playing_state(ctrl) is True
        assert ctrl._last_idle is False
        # fake MPV 记录与请求顺序一致：100 次 loadfile，歌曲按序
        loadfiles = [c[1] for c in fake.calls if c[0] == "loadfile"]
        assert len(loadfiles) == 100
        assert loadfiles == [s["path"] for s in songs]
        # 代次递增 100 次（唯一咽喉），无多余残留
        assert ctrl._play_request_id == 100
        # 队列保持完整（play_song 只追加不丢歌）
        assert len(ctrl.queue._queue) == 100

    def test_100_next_locked_requests_advance_and_do_not_leak_state(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        songs = [_local_song(tmp_path, i, f"N{i}") for i in range(150)]
        for s in songs:
            ctrl.queue.add(s)
        ctrl.queue.next()  # 指针 → N0

        with ctrl._lock:
            for _ in range(100):
                ctrl._next_locked()
                assert ctrl._is_switching_track is False

        # 100 次切歌后指针与当前曲目一致（最后一个有效请求决定最终曲目）
        assert ctrl._current_song is songs[100]
        assert ctrl.queue.get_current() is songs[100]
        assert _playing_state(ctrl) is True
        loadfiles = [c[1] for c in fake.calls if c[0] == "loadfile"]
        assert loadfiles == [songs[i]["path"] for i in range(1, 101)]


class TestStaleOnlineLateReturn:
    """B：旧在线请求在取流阶段被 gate 阻塞，新请求完成后才返回 →
    旧请求必须被代次防线丢弃（无锁快速检查路径），不覆盖新状态。"""

    def test_stale_online_worker_does_not_override_newer_song(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl._resolver = MagicMock()  # 直取路径优先，resolver 仅兜底（本例不触发）
        in_gate = threading.Event()
        release = threading.Event()
        stale_url = "http://stale.example/old.mp3"

        def gated_get_play_url(track, quality="high"):
            in_gate.set()
            release.wait(timeout=5)
            return {"url": stale_url, "headers": None}

        ctrl.online_manager.get_play_url.side_effect = gated_get_play_url

        song_a = _online_song("OldSong", track_id="old-1")
        song_b = _local_song(tmp_path, 2, "NewSong")

        # A 的 worker 线程（真实 _resolve_online_worker 执行链）
        req_a = ctrl._play_request_id
        worker = threading.Thread(
            target=ctrl._resolve_online_worker,
            args=(song_a, "OldSong Artist", req_a), daemon=True,
            name="diag-old-worker")
        worker.start()
        assert in_gate.wait(timeout=5), "旧请求应进入取流阶段"

        # 新请求（本地 play_song）先成功：递增代次、loadfile、更新状态
        ctrl.play_song(song_b)
        assert ctrl._current_song is song_b

        # 释放旧请求 → 无锁快速检查发现代次过期，不得 loadfile 旧 URL
        release.set()
        worker.join(timeout=5)
        assert not worker.is_alive(), "旧 worker 应已结束"

        assert [c[1] for c in fake.calls if c[0] == "loadfile"] == [song_b["path"]]
        assert stale_url not in [c[1] for c in fake.calls if c[0] == "loadfile"]
        assert ctrl._current_song is song_b
        assert _playing_state(ctrl) is True
        assert ctrl._is_switching_track is False
        # 旧 worker 结束不得改写代次后的任何状态
        assert ctrl._play_request_id == 1  # 仅 play_song 递增一次


class TestOldFailureNewSuccess:
    """C：旧请求 loadfile 失败（异常窗口内代次被接管）+ 新请求随后成功 →
    旧清理不清新状态，终态为完整成功态。"""

    def test_failed_request_then_new_success_state_is_clean(self, tmp_path):
        fake = FirstFailThenOkMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        fake.bind(ctrl)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        song_c = _local_song(tmp_path, 3, "C")
        for s in (song_a, song_b, song_c):
            ctrl.queue.add(s)
        ctrl.queue.next()  # 指针 → A
        ctrl._current_song = song_a

        # 旧请求：切到 B，loadfile 抛异常且代次已被并发接管
        with ctrl._lock:
            ctrl._next_locked()
        # 旧异常清理被代次守卫阻止：切换态仍由新请求接管（保持 True）
        assert ctrl._is_switching_track is True

        # 新请求随后成功：切到 C
        with ctrl._lock:
            ctrl._next_locked()
        assert ctrl._is_switching_track is False
        assert ctrl._current_song is song_c
        assert ctrl.queue.get_current() is song_c
        assert _playing_state(ctrl) is True
        # fake 记录：B loadfile 失败（已记录）+ C loadfile 成功（最后）
        loadfiles = [c[1] for c in fake.calls if c[0] == "loadfile"]
        assert loadfiles[-1] == song_c["path"]
        assert len(loadfiles) == 2
        assert loadfiles[0] == song_b["path"]
