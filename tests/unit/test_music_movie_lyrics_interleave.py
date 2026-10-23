# -*- coding: utf-8 -*-
"""音乐 / 电影 / 歌词窗口交错验收测试（确定性构造，非随机压力）。

覆盖：
A. 电影停止后音乐事件 no-op（旧事件不覆盖停止态）；
B. 歌词窗口关闭 → 后台迟到结果：不 emit、无未处理异常；
   窗口真正关闭（closeEvent）后同一实例重开 → 歌词应恢复加载
   （当前实现无 _closed 复位 → xfail 复现缺陷）；
C. 音乐模块停止后，自动切歌 / 延迟跳过 Timer 回调不得重新播放。

全部使用可控 Fake + gate Event + worker.join()；不启动真实 mpv、
不创建真实 UI 主程序（B 用 QApplication offscreen + 事件循环 pump）。
"""
import threading
import time

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.core.command_bus import CommandBus
from app.core.event_bus import Event, EventBus, EventType
from app.core.state_store import StateNamespace
from app.music.controller import MusicController
from app.ui.widgets.lyrics_window import LyricsWindow


# ============================ 通用工具 ============================

@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def pump(qapp, n=60):
    for _ in range(n):
        qapp.processEvents()
        time.sleep(0.005)


# ============================ A. 音乐事件 × 电影停止 ============================

class MovieFakeMpv:
    def __init__(self, running=True, stop_result=True):
        self.calls = []
        self._running = running
        self._stop_result = stop_result
        self._properties = {}

    def is_running(self):
        return self._running

    def stop(self):
        self.calls.append(("stop",))
        return self._stop_result

    def stop_process(self):
        self.calls.append(("stop_process",))

    def loadfile(self, url, mode="replace"):
        self.calls.append(("loadfile", url, mode))
        return {"error": "success"}

    def set_property(self, name, value):
        self.calls.append(("set_property", name, value))
        self._properties[name] = value

    def pause(self):
        self.calls.append(("pause",))

    def play(self):
        self.calls.append(("play",))


class MovieFakeStore:
    def __init__(self):
        self.data = {}

    def set(self, namespace, key, value):
        self.data[(namespace, key)] = value


def _make_movie_config(tmp_path):
    return {
        "music.mpv_path": "mpv",
        "movie.mpv_ipc_pipe": r"\\.\pipe\test_movie_interleave",
        "movie.watch_later_dir": str(tmp_path / "watch_later"),
        "app.data_dir": str(tmp_path / "data"),
        "movie.embed": False,
        "movie.auto_next": True,
    }


def make_movie_controller(env, fake, config):
    fake_store = MovieFakeStore()
    fake_bus = EventBus()
    env.enter_context(patch("app.movie.movie_controller.MpvController", return_value=fake))
    env.enter_context(patch("app.movie.movie_controller.get_config", return_value=config))
    env.enter_context(patch("app.movie.movie_controller.get_event_bus", return_value=fake_bus))
    env.enter_context(patch("app.core.command_bus.get_command_bus", return_value=CommandBus()))
    env.enter_context(patch("app.movie.movie_controller.get_state_store", return_value=fake_store))
    from app.movie.movie_controller import MovieController
    mc = MovieController()
    return mc, fake_store, fake_bus


@pytest.fixture
def env():
    from contextlib import ExitStack
    with ExitStack() as stack:
        yield stack


def test_music_event_after_stop_is_noop(tmp_path, env):
    """电影 stop 明确成功后，后续音乐播放事件必须 no-op：
    不调 mpv.pause、不写 paused、不改变停止终态（旧事件不覆盖新状态）。"""
    fake = MovieFakeMpv(running=True, stop_result=True)
    mc, store, bus = make_movie_controller(env, fake, _make_movie_config(tmp_path))
    mc._current = {"vod_name": "测试剧", "play_url": "1$http://x/1#2$http://x/2"}
    mc._current_index = 0
    mc._last_idle = False

    mc.stop()
    assert mc._current is None
    assert store.data[(StateNamespace.MOVIE, "playing")] is False

    pause_calls_before = fake.calls.count(("pause",))
    bus.publish(Event(event_type=EventType.MUSIC_TRACK_CHANGED, data={}, source="test"))
    bus.publish(Event(event_type=EventType.MUSIC_PLAY, data={}, source="test"))

    # 停止后音乐事件：无 pause 调用、paused 保持停止终态（False）、
    # 状态保持停止态（旧事件不覆盖新状态）
    assert fake.calls.count(("pause",)) == pause_calls_before
    assert store.data.get((StateNamespace.MOVIE, "paused")) is False
    assert mc._current is None
    assert store.data[(StateNamespace.MOVIE, "playing")] is False
    assert mc._pausing is False


def test_music_event_after_unconfirmed_stop_keeps_playing_semantics(tmp_path, env):
    """stop 结果未确认（None）时播放权保留；音乐事件仍可暂停电影
    （保留进度），不破坏"停止失败仍视为播放中"的既有语义。"""
    fake = MovieFakeMpv(running=True, stop_result=None)
    mc, store, bus = make_movie_controller(env, fake, _make_movie_config(tmp_path))
    mc._current = {"vod_name": "测试剧", "play_url": "1$http://x/1"}
    mc._current_index = 0
    mc._last_idle = False
    store.data[(StateNamespace.MOVIE, "playing")] = True

    mc.stop()
    assert mc._current is not None          # 未确认 → 保留上下文

    bus.publish(Event(event_type=EventType.MUSIC_TRACK_CHANGED, data={}, source="test"))
    assert ("pause",) in fake.calls          # 仍在"播放中" → 音乐事件暂停电影
    assert store.data[(StateNamespace.MOVIE, "paused")] is True
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    assert mc._pausing is False


# ============================ B. 歌词窗口关闭 × 后台结果 ============================

def _online_song(title="歌A", track_id="t1"):
    return {
        "title": title, "artist": "艺A", "album": "专A",
        "path": "http://x.example/a.mp3", "source_name": "src",
        "track_id": track_id, "extra": {"_raw": {"id": track_id}},
    }


class _Event:
    def __init__(self, song):
        self.data = {"song": song}


class FakeOnlineManager:
    def __init__(self):
        self.result = "[00:01.00]行1"
        self.exc = None
        self.block = None
        self.started = threading.Event()
        self.calls = []

    def get_lyric(self, track):
        self.calls.append(track)
        self.started.set()
        if self.block is not None:
            self.block.wait()
        if self.exc is not None:
            raise self.exc
        return self.result


class FakeController:
    def __init__(self):
        self.online_manager = FakeOnlineManager()
        self.event_bus = EventBus()
        self._current = None

    def get_current_song(self):
        """start() 的 _load_current() 与 200ms _sync 轮询依赖；None → 清空占位。"""
        return self._current


def test_close_then_late_result_no_emit_no_exception(qapp):
    """关闭窗口 → 释放后台取词 → worker.join()：结果不 emit（无回调）、
    worker 无未处理异常、窗口状态保持关闭态（不写入）。"""
    ctrl = FakeController()
    mgr = ctrl.online_manager
    gate = threading.Event()
    mgr.block = gate
    win = LyricsWindow(ctrl)
    emitted = []
    win._lyric_fetched.connect(
        lambda seq, snapshot, lines: emitted.append((seq, snapshot, lines)))

    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    assert mgr.started.wait(2)
    win.close()                     # closeEvent → _closed=True + stop()
    gate.set()                      # 迟到结果到达

    pump(qapp)
    assert emitted == []            # 未 emit 到已关闭窗口
    assert win._lines == []
    assert win._title == ""


def test_close_late_result_does_not_override_new_instance_song(qapp):
    """关闭旧实例的迟到结果不得影响新实例（新建窗口）的新歌加载。"""
    ctrl = FakeController()
    mgr = ctrl.online_manager
    gate = threading.Event()
    mgr.block = gate
    win_old = LyricsWindow(ctrl)
    win_old._on_track_changed(_Event(_online_song("旧歌", "old")))
    assert mgr.started.wait(2)
    win_old.close()

    mgr.started.clear()
    mgr.block = None
    mgr.result = "[00:02.00]新歌歌词"
    win_new = LyricsWindow(ctrl)     # 新建实例：_closed=False
    win_new._on_track_changed(_Event(_online_song("新歌", "new")))
    pump(qapp)

    gate.set()                       # 旧实例迟到结果（被 _closed 丢弃）
    pump(qapp)
    assert win_new._title == "新歌"
    assert win_new._lines == [(2.0, "新歌歌词")]
    assert win_old._title == ""      # 旧实例保持关闭态


def test_reopen_same_instance_after_close_should_reload(qapp):
    """真实 UI 路径（music_panel 复用实例）：用户点 X 关闭歌词窗口后再次打开
    （start()+show()，同实例），歌词加载必须恢复。修复：start() 复位 _closed。"""
    ctrl = FakeController()
    win = LyricsWindow(ctrl)
    win._on_track_changed(_Event(_online_song("歌A", "t1")))
    pump(qapp)
    assert win._title == "歌A"       # 首轮正常

    win.close()                      # 用户点 X → closeEvent → _closed=True
    ctrl._current = _online_song("歌B", "t2")
    win.start()                      # 真实重开路径：start + show（同实例）
    win.show()
    pump(qapp)                       # start() 内 _load_current 请求歌B → worker → 应用

    assert win._title == "歌B"       # 修复后：重开歌词恢复加载
    assert win._lines == [(1.0, "行1")]
    assert win._closed is False      # start() 已复位关闭守卫


def test_reopen_local_lyric_loads(qapp, tmp_path):
    """同一实例关闭后重开：本地歌词路径（path 为本地文件）可正常加载
    （修复前 _on_lyric_requested 被 _closed 挡住 → 标题都显示不出来）。"""
    ctrl = FakeController()
    lrc_path = tmp_path / "本地歌.lrc"
    lrc_path.write_text("[00:01.00]本地歌词行", encoding="utf-8")
    local_song = {
        "title": "本地歌", "artist": "艺", "album": "",
        "path": str(lrc_path), "source_name": "local",
        "track_id": "", "extra": {},
    }
    win = LyricsWindow(ctrl)
    win._on_track_changed(_Event(local_song))
    pump(qapp)
    assert win._title == "本地歌"

    win.close()                       # 点 X 关闭 → _closed=True
    ctrl._current = local_song         # 重开时当前歌为本地歌
    win.start()                       # 重开（同实例）
    win.show()
    win._on_track_changed(_Event(local_song))
    pump(qapp)

    assert win._closed is False
    assert win._title == "本地歌"      # 本地分支可达并应用
    assert win._lines == [(1.0, "本地歌词行")]


def test_old_request_after_reopen_does_not_override_new_song(qapp):
    """首次打开产生的旧在线请求，在关闭并重开后迟到返回：不得覆盖重开后的新歌歌词。"""
    ctrl = FakeController()
    mgr = ctrl.online_manager
    gate = threading.Event()
    mgr.block = gate
    win = LyricsWindow(ctrl)
    win._on_track_changed(_Event(_online_song("旧歌", "old")))
    assert mgr.started.wait(2)

    win.close()                       # 关闭（旧请求仍被 gate 阻塞在 worker 中）
    mgr.block = None                  # 重开前放行旧请求 → 关闭态下应被 _closed 丢弃
    ctrl._current = _online_song("新歌", "new")
    win.start()
    win.show()

    # 重开后发起新歌请求（新代次）
    win._on_track_changed(_Event(_online_song("新歌", "new")))
    pump(qapp)
    assert win._title == "新歌"

    # 显式触发"关闭后重开前"旧 worker 的迟到 emit（_closed 已复位、seq 过期 → 丢弃）
    win._on_lyric_fetched(1, {"key": "old|src|old", "title": "旧歌"}, [(1.0, "旧歌词")])
    pump(qapp)
    assert win._title == "新歌"
    assert win._lines == [(1.0, "行1")]      # 旧请求结果未覆盖新歌


def test_reopen_twice_still_loads(qapp):
    """连续关闭、重开两次：第二次同样正常加载（_closed 每次都由 start() 复位）。"""
    ctrl = FakeController()
    win = LyricsWindow(ctrl)
    for i, title in enumerate(("第一轮", "第二轮")):
        ctrl._current = _online_song(title, f"t{i}")
        win._on_track_changed(_Event(_online_song(title, f"t{i}")))
        pump(qapp)
        assert win._title == title
        win.close()
        win.start()
        win.show()
    # 第三次（含两次重开之后）仍正常
    ctrl._current = _online_song("第三轮", "t2")
    win._on_track_changed(_Event(_online_song("第三轮", "t2")))
    pump(qapp)
    assert win._title == "第三轮"
    assert win._lines == [(1.0, "行1")]


# ============================ C. 停止后 Timer 回调不重启播放 ============================

class MusicFakeMpv:
    def __init__(self):
        self.calls = []
        self._properties = {}

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

    def set_property(self, name, value):
        self._properties[name] = value

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

    def get_filename(self):
        return None

    def is_idle(self):
        return False


def _make_music_ctrl(tmp_path, fake):
    cfg = {
        "database": str(tmp_path / "stop_timer.db"),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_stop_timer",
        "default_volume": 70,
    }
    with patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        ctrl = MusicController(cfg, mpv_adapter=fake)
        ctrl.lx_connected = False
        ctrl._running = True
        return ctrl


def _online_song_dict(title="T", track_id="t1"):
    return {"id": -1, "title": title, "artist": "A", "path": "http://x.example/a.mp3",
            "duration": 10.0, "source_type": "online",
            "source_name": "src", "track_id": track_id, "extra": {}}


def test_auto_next_after_stop_does_not_play(tmp_path):
    """模块停止后自动切歌 Timer 回调（_auto_next）必须直接返回：不 loadfile、
    不改变曲目与播放状态。"""
    fake = MusicFakeMpv()
    ctrl = _make_music_ctrl(tmp_path, fake)
    song = _online_song_dict()
    ctrl.queue.add(song)
    ctrl.queue.next()
    ctrl._current_song = song
    ctrl._is_switching_track = True

    ctrl.stop()                       # _running = False
    ctrl._auto_next()                 # 模拟 Timer 到期回调

    # 断言使用测试隔离的 fake 调用与本地状态（state_store 为全局单例，
    # 会被其他音乐测试残留污染，不作为本用例断言依据）
    assert [c for c in fake.calls if c[0] in ("loadfile", "play", "stop")] == []
    assert ctrl._current_song is song  # 停止态不被回调改写


def test_do_skip_online_after_stop_does_not_play(tmp_path):
    fake = MusicFakeMpv()
    ctrl = _make_music_ctrl(tmp_path, fake)
    song = _online_song_dict()
    ctrl.queue.add(song)
    ctrl.queue.next()
    ctrl._current_song = song
    ctrl._is_switching_track = True

    ctrl.stop()
    ctrl._do_skip_online(song)        # 模拟 0.2s 延迟跳过 Timer 到期

    assert [c for c in fake.calls if c[0] == "loadfile"] == []
    assert ("stop",) not in fake.calls
    assert ctrl._current_song is song


def test_do_skip_bad_after_stop_does_not_play(tmp_path):
    fake = MusicFakeMpv()
    ctrl = _make_music_ctrl(tmp_path, fake)
    song = _online_song_dict()
    ctrl.queue.add(song)
    ctrl.queue.next()
    ctrl._current_song = song
    ctrl._is_switching_track = True

    ctrl.stop()
    ctrl._do_skip_bad(song)           # 模拟坏文件跳过 Timer 到期

    assert [c for c in fake.calls if c[0] == "loadfile"] == []
    assert ctrl._current_song is song
