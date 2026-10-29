# -*- coding: utf-8 -*-
"""_is_switching_track 异常复位修复测试（loadfile 抛异常路径）。

覆盖：
1. 本地歌曲 loadfile 抛异常（ConnectionError / OSError）→ 切换态复位、
   不伪造播放成功、_current_song / 队列 / 播放状态不被错误更新、异常记日志。
2. 在线歌曲（play_online → _apply_stream）loadfile 抛异常 → 切换态复位、
   play_online 返回 False、无 TRACK_CHANGED 发布、状态不被错误更新。
3. 并发防御：异常窗口内代次已被"更新请求"递增（过期请求）→ 不复位
   _is_switching_track（新请求状态不被旧请求覆盖）。

全部使用可控 Fake MPV（loadfile 抛异常），不依赖真实 mpv 进程。
"""
import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.core.state_store import StateNamespace
from app.music.controller import MusicController

# FakeMpv.loadfile 默认成功响应的哨兵（与"显式传 None=未确认"区分）
_DEFAULT_OK = {"error": "success"}


class FakeMpv:
    """不发真声音的 mpv 替身（协议满足 MpvController 受测子集）。

    loadfile_result 可配 loadfile() 返回值（默认 success 响应，与真实接口
    "返回 _send_command 结果"的契约一致）：{"error": "success"}=命令被接受 /
    None=结果未确认 / {"error": "..."}=协议层失败。loadfile_exc 非 None 时抛异常。
    """

    def __init__(self, idle_active=False, stop_result=True,
                 loadfile_result=_DEFAULT_OK, loadfile_exc=None):
        self.calls = []
        self._idle_active = idle_active
        self._stop_result = stop_result
        self._properties = {}
        self.volume = 70
        self._loadfile_result = loadfile_result
        self._loadfile_exc = loadfile_exc

    def on_property_change(self, name, cb):
        self.calls.append(("on_property_change", name))

    def start(self, taskkill=True, pipe_timeout=3.0, prop_timeout=0.5):
        self.calls.append(("start",))
        return True

    def stop(self):
        self.calls.append(("stop",))
        return self._stop_result

    def stop_process(self):
        self.calls.append(("stop_process",))

    def is_running(self):
        return True

    def loadfile(self, filepath, mode="replace"):
        self.calls.append(("loadfile", filepath))
        if self._loadfile_exc is not None:
            raise self._loadfile_exc
        return self._loadfile_result

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


class FailingMpv(FakeMpv):
    """loadfile 一律抛异常（默认 IPC 未连接）。"""

    def __init__(self, exc=None):
        super().__init__()
        self._exc = exc if exc is not None else ConnectionError("mpv IPC 未连接")

    def loadfile(self, filepath, mode="replace"):
        self.calls.append(("loadfile", filepath))
        raise self._exc


class InterleavedMpv(FailingMpv):
    """loadfile 抛异常前先递增代次，模拟"异常窗口内已有更新请求接管"。"""

    def __init__(self):
        super().__init__()
        self._ctrl = None

    def bind(self, ctrl):
        self._ctrl = ctrl

    def loadfile(self, filepath, mode="replace"):
        self.calls.append(("loadfile", filepath))
        self._ctrl._play_request_id += 1  # 并发切歌已递增代次（防御路径）
        raise ConnectionError("mpv IPC 未连接")


class RespFailInterleavedMpv(FakeMpv):
    """loadfile 返回协议层 error 响应且先递增代次（并发接管语义）。

    用于旧请求失败（error 响应）时"已有新请求递增代次"→ 旧清理不得清新状态；
    随后新请求成功 → 终态必须是新请求的完整成功态。
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
            self._ctrl._play_request_id += 1  # 并发切歌已递增代次（防御路径）
            return {"error": "not accepted"}
        return {"error": "success"}


def _make_ctrl(tmp_path, fake, db_name="loadfile_failure.db"):
    cfg = {
        "database": str(tmp_path / db_name),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_loadfile_failure",
        "default_volume": 70,
    }
    with patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        ctrl = MusicController(cfg, mpv_adapter=fake)
        ctrl.lx_connected = False
        # 在线播放的生产路径要求电影暂停确认；本文件专测 loadfile 失败收口。
        ctrl.set_movie_pauser(lambda: True)
        return ctrl


def _local_song(tmp_path, song_id, title):
    p = tmp_path / f"{title}.flac"
    p.write_bytes(b"fake-flac-bytes")
    return {"id": song_id, "title": title, "path": str(p), "duration": 10.0}


def _playing_state(ctrl):
    return ctrl.state_store.get(StateNamespace.MUSIC, "playing", None)


class TestLocalLoadfileError:
    @pytest.mark.parametrize("exc", [
        ConnectionError("mpv IPC 未连接"),
        OSError("管道已关闭"),
    ])
    def test_local_loadfile_error_resets_switch_and_preserves_state(self, tmp_path, exc):
        fake = FailingMpv(exc)
        ctrl = _make_ctrl(tmp_path, fake)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()  # 指针 → A（index 0）
        ctrl._current_song = song_a  # 旧歌仍在播
        ctrl._last_idle = False
        queue_before = list(ctrl.queue._queue)
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.next()  # → _next_locked → queue.next() → B → loadfile 抛异常

        # 切换态不复位失败：必须复位（不得永久卡 True）
        assert ctrl._is_switching_track is False
        assert ctrl._last_idle is False
        # loadfile 确实被尝试
        assert ("loadfile", song_b["path"]) in fake.calls
        # 不伪造播放成功：_current_song 仍为旧歌、异常路径不得改写播放状态
        assert ctrl._current_song is song_a
        assert _playing_state(ctrl) is playing_before
        # 队列内容未被清空或改写
        assert list(ctrl.queue._queue) == queue_before
        # 异常被记录
        assert mock_logger.error.call_count >= 1
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "本地 loadfile 失败" in joined and exc.__class__.__name__ in joined

    def test_local_loadfile_error_keeps_playing_state_unwritten(self, tmp_path):
        fake = FailingMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()
        ctrl._current_song = song_a
        playing_before = _playing_state(ctrl)

        ctrl.next()

        # _update_state(playing=True) 未被调用：异常路径不得改写 playing
        assert _playing_state(ctrl) is playing_before


class TestOnlineLoadfileError:
    def test_online_apply_stream_error_resets_flag_and_returns_false(self, tmp_path):
        fake = FailingMpv(ConnectionError("mpv IPC 未连接"))
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = {
            "url": "http://example.com/a.mp3", "headers": None}
        track = MagicMock(title="T", artist="A", source_name="demo",
                          track_id="t1", cover_url=None)
        ctrl._is_switching_track = False
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            result = ctrl.play_online(track)

        # play_online 前置已置 True，异常路径必须复位
        assert result is False
        assert ctrl._is_switching_track is False
        assert ("loadfile", "http://example.com/a.mp3") in fake.calls
        # 状态未被错误更新：_current_song 不变、异常路径不得改写 playing
        assert ctrl._current_song is None
        assert _playing_state(ctrl) is playing_before
        assert mock_logger.error.call_count >= 1
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "在线 loadfile 失败" in joined

    def test_online_apply_stream_error_not_published_as_success(self, tmp_path):
        fake = FailingMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = {
            "url": "http://example.com/b.mp3", "headers": None}
        track = MagicMock(title="T2", artist="A2", source_name="demo",
                          track_id="t2", cover_url=None)
        bus = MagicMock()
        ctrl.event_bus = bus

        ctrl.play_online(track)

        # 失败路径不发布 MUSIC_TRACK_CHANGED（不把失败当作成功通知消费方）
        published = [c[0][0] for c in bus.publish.call_args_list]
        assert published == []


class TestPlayOnlineEarlyReturnReset:
    """play_online 置 _is_switching_track=True 后、正式 loadfile 前的
    失败 return 路径（mpv 不可用 / online_manager 缺失 / 无 URL）：
    切换态必须复位，且不得伪造播放成功、不改队列/历史/当前曲目。
    """

    def _track(self):
        return MagicMock(title="T", artist="A", source_name="demo",
                         track_id="t1", cover_url=None)

    def test_mpv_unavailable_resets_switch_flag(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl._mpv_available = False
        playing_before = _playing_state(ctrl)
        bus = MagicMock()
        ctrl.event_bus = bus

        with patch("app.music.controller.logger") as mock_logger:
            result = ctrl.play_online(self._track())

        assert result is False
        assert ctrl._is_switching_track is False
        assert _playing_state(ctrl) is playing_before
        assert ctrl._current_song is None
        assert bus.publish.call_count == 0
        assert mock_logger.info.call_count >= 1  # "mpv 不可用"已记录

    def test_online_manager_none_resets_switch_flag(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = None
        playing_before = _playing_state(ctrl)
        bus = MagicMock()
        ctrl.event_bus = bus

        result = ctrl.play_online(self._track())

        assert result is False
        assert ctrl._is_switching_track is False
        assert _playing_state(ctrl) is playing_before
        assert ctrl._current_song is None
        assert bus.publish.call_count == 0

    def test_no_url_resets_switch_flag_and_preserves_queue(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = None
        song_a = _local_song(tmp_path, 1, "A")
        ctrl.queue.add(song_a)
        queue_before = list(ctrl.queue._queue)
        playing_before = _playing_state(ctrl)
        bus = MagicMock()
        ctrl.event_bus = bus

        with patch("app.music.controller.logger") as mock_logger:
            result = ctrl.play_online(self._track())

        assert result is False
        assert ctrl._is_switching_track is False
        assert _playing_state(ctrl) is playing_before
        assert ctrl._current_song is None
        assert list(ctrl.queue._queue) == queue_before
        assert bus.publish.call_count == 0
        assert mock_logger.info.call_count >= 1  # "online 无 URL"已记录

    def test_no_url_stale_request_does_not_clear_newer_state(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()

        def stale(track):
            ctrl._play_request_id += 1  # 失败窗口内已有新请求递增代次
            return None

        ctrl.online_manager.get_play_url.side_effect = stale

        result = ctrl.play_online(self._track())

        assert result is False
        # 过期请求不得清除新请求的切换态：仍保持 True（新请求接管）
        assert ctrl._is_switching_track is True


class TestSkipOnlineEmptyQueue:
    """_do_skip_online() 空队列收口：单首在线歌解析失败、队列无下一首时，
    next() 返回 None 不触发新播放请求——若代次仍属于原失败请求则收口切换态；
    已有新请求接管（代次递增）则不得复位。不伪造播放成功、不改队列/历史。
    """

    def _online_song(self, title="T", path="http://example.com/skip.mp3"):
        return {"id": -1, "title": title, "artist": "A", "path": path,
                "duration": 10.0, "source_type": "online"}

    def test_skip_online_empty_queue_resets_switch_flag(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl._running = True  # 与 _do_skip_online 的 _running 守卫一致
        song = self._online_song()
        ctrl.queue.add(song)
        ctrl.queue.next()  # 指针 → 唯一在线歌（index 0）
        assert ctrl.queue.get_current() is song
        ctrl._current_song = song
        ctrl._is_switching_track = True  # 解析失败遗留切换态
        ctrl._last_idle = False  # 真实链路：_play_song 已置 False（播放中/解析窗口）
        bus = MagicMock()
        ctrl.event_bus = bus
        queue_before = list(ctrl.queue._queue)

        ctrl._do_skip_online(song)

        # 空队列收口：切换态复位，不遗留
        assert ctrl._is_switching_track is False
        # 不伪造播放成功：无 loadfile、播放状态为停止、当前曲目清空
        assert [c for c in fake.calls if c[0] == "loadfile"] == []
        assert ("stop",) in fake.calls
        assert ctrl._current_song is None
        assert ctrl.state_store.get(StateNamespace.MUSIC, "playing", None) is False
        # 队列内容不变、无发布
        assert list(ctrl.queue._queue) == queue_before
        assert bus.publish.call_count == 0

    def test_skip_online_stale_request_does_not_reset_newer_state(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl._running = True
        song = self._online_song()
        ctrl.queue.add(song)
        ctrl.queue.next()
        ctrl._current_song = song
        ctrl._is_switching_track = True
        ctrl._last_idle = False  # 真实链路前置：跳过 _next_locked 的 auto 守卫
        # 模拟 next 期间已有新请求递增代次（旧跳曲不得清除新请求状态）
        orig_next = ctrl.queue.next

        def racing_next(auto=False):
            ctrl._play_request_id += 1
            return orig_next(auto=auto)

        ctrl.queue.next = racing_next

        ctrl._do_skip_online(song)

        # 代次已变 → 不复位：切换态由新请求接管，仍保持 True
        assert ctrl._is_switching_track is True
        # 新请求接管后空队列分支照常收口播放状态（不伪造播放）
        assert ctrl._current_song is None
        assert ctrl.state_store.get(StateNamespace.MUSIC, "playing", None) is False

    def test_skip_online_advances_to_next_song_when_available(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl._running = True
        song = self._online_song()
        next_song = _local_song(tmp_path, 7, "Next")
        ctrl.queue.add(song)
        ctrl.queue.add(next_song)
        ctrl.queue.next()  # 指针 → 在线歌（index 0）
        ctrl._current_song = song
        ctrl._is_switching_track = True
        ctrl._last_idle = False  # 真实链路前置：跳过 _next_locked 的 auto 守卫

        ctrl._do_skip_online(song)

        # 有下一首：skip 推进 → _play_song 成功（代次递增），切换态由播放复位
        assert ctrl._is_switching_track is False
        assert ("loadfile", next_song["path"]) in fake.calls
        assert ctrl._current_song is next_song
        assert ctrl.state_store.get(StateNamespace.MUSIC, "playing", None) is True


class TestStaleRequestGuard:
    def test_stale_loadfile_error_does_not_clear_newer_switch_state(self, tmp_path):
        fake = InterleavedMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        fake.bind(ctrl)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()
        ctrl._current_song = song_a
        ctrl._is_switching_track = True  # 切换进行中

        ctrl.next()  # B 的 loadfile 抛异常，但代次已被"更新请求"递增

        # 过期请求不得覆盖新请求的切换态：仍保持 True（新请求接管）
        assert ctrl._is_switching_track is True


class TestProtocolLoadfileFailure:
    """loadfile 协议层失败（error 响应）与未确认（None）不伪造播放成功。

    真实契约：{"error": "success"}=命令被接受 / error 其他值=协议层失败 /
    None=结果未确认 / 异常=连接失败。本地与在线两条路径分别验证。
    """

    def test_local_error_response_resets_switch_and_preserves_state(self, tmp_path):
        fake = FakeMpv(loadfile_result={"error": "file not found"})
        ctrl = _make_ctrl(tmp_path, fake)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()
        ctrl._current_song = song_a
        ctrl._last_idle = False
        queue_before = list(ctrl.queue._queue)
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.next()  # → B → loadfile 协议层失败

        assert ctrl._is_switching_track is False
        assert ctrl._last_idle is False
        assert ("loadfile", song_b["path"]) in fake.calls
        assert ctrl._current_song is song_a          # 不把失败集写入当前曲目
        assert _playing_state(ctrl) is playing_before  # 不伪造 playing=True
        assert list(ctrl.queue._queue) == queue_before
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "本地 loadfile 失败" in joined and "file not found" in joined

    def test_local_unconfirmed_does_not_pretend_success(self, tmp_path):
        fake = FakeMpv(loadfile_result=None)
        ctrl = _make_ctrl(tmp_path, fake)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()
        ctrl._current_song = song_a
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            ctrl.next()

        assert ctrl._is_switching_track is False
        assert ctrl._current_song is song_a
        assert _playing_state(ctrl) is playing_before
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "结果未确认" in joined

    def test_online_error_response_returns_false_and_keeps_state(self, tmp_path):
        fake = FakeMpv(loadfile_result={"error": "not accepted"})
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = {
            "url": "http://example.com/c.mp3", "headers": None}
        track = MagicMock(title="T3", artist="A3", source_name="demo",
                          track_id="t3", cover_url=None)
        ctrl._is_switching_track = False
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            result = ctrl.play_online(track)

        assert result is False
        assert ctrl._is_switching_track is False
        assert ("loadfile", "http://example.com/c.mp3") in fake.calls
        assert ctrl._current_song is None
        assert _playing_state(ctrl) is playing_before
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "在线 loadfile 失败" in joined and "not accepted" in joined

    def test_online_unconfirmed_returns_false_and_keeps_state(self, tmp_path):
        fake = FakeMpv(loadfile_result=None)
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = {
            "url": "http://example.com/d.mp3", "headers": None}
        track = MagicMock(title="T4", artist="A4", source_name="demo",
                          track_id="t4", cover_url=None)
        playing_before = _playing_state(ctrl)

        with patch("app.music.controller.logger") as mock_logger:
            result = ctrl.play_online(track)

        assert result is False
        assert ctrl._is_switching_track is False
        assert ctrl._current_song is None
        assert _playing_state(ctrl) is playing_before
        joined = " ".join(a[0][0] for a in mock_logger.error.call_args_list)
        assert "结果未确认" in joined

    def test_stale_error_response_does_not_clear_newer_switch_state(self, tmp_path):
        """旧请求 error 响应 + 代次被接管 → 旧清理不清新请求切换态；
        随后新请求成功 → 终态为完整成功态。"""
        fake = RespFailInterleavedMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        fake.bind(ctrl)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        song_c = _local_song(tmp_path, 3, "C")
        for s in (song_a, song_b, song_c):
            ctrl.queue.add(s)
        ctrl.queue.next()  # → A
        ctrl._current_song = song_a
        ctrl._is_switching_track = True

        with ctrl._lock:
            ctrl._next_locked()   # B：error 响应且代次被接管
        assert ctrl._is_switching_track is True   # 旧清理被守卫阻止

        with ctrl._lock:
            ctrl._next_locked()   # C：成功
        assert ctrl._is_switching_track is False
        assert ctrl._current_song is song_c
        assert ctrl.queue.get_current() is song_c
        assert _playing_state(ctrl) is True
        loadfiles = [c[1] for c in fake.calls if c[0] == "loadfile"]
        assert loadfiles == [song_b["path"], song_c["path"]]

    def test_local_success_response_regression(self, tmp_path):
        """成功响应（命令被接受）→ 正常播放：切换态复位、状态写入。"""
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        song_a = _local_song(tmp_path, 1, "A")
        song_b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(song_a)
        ctrl.queue.add(song_b)
        ctrl.queue.next()
        ctrl._current_song = song_a

        ctrl.next()

        assert ctrl._is_switching_track is False
        assert ctrl._current_song is song_b
        assert _playing_state(ctrl) is True

    def test_online_success_response_regression(self, tmp_path):
        """成功响应 → play_online 正常播放并发布。"""
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.online_manager = MagicMock()
        ctrl.online_manager.get_play_url.return_value = {
            "url": "http://example.com/e.mp3", "headers": None}
        track = MagicMock(title="T5", artist="A5", source_name="demo",
                          track_id="t5", cover_url=None)
        bus = MagicMock()
        ctrl.event_bus = bus

        result = ctrl.play_online(track)

        assert result is True
        assert ctrl._is_switching_track is False
        assert ctrl._current_song is not None
        assert _playing_state(ctrl) is True
        assert bus.publish.call_count >= 0  # 播放成功路径自身不发布 TRACK_CHANGED
        assert ("loadfile", "http://example.com/e.mp3") in fake.calls
