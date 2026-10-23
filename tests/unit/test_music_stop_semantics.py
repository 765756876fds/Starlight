# -*- coding: utf-8 -*-
"""第五阶段：停止类语音指令语义（MUSIC_STOP / stop_current() / 自动续播竞争守卫）。

覆盖：
1. Intent 解析：停止音乐类短语 → music.stop；停止说话 → system.stop_tts；
   暂停音乐 → music.pause；停止播放 → system.stop_tts（不回归）。
2. Controller stop_current()：停止当前播放、_current_song 清空、playing=False、
   队列保留且指针不推进、mpv.stop 调用、_last_idle=True。
3. 停止后主动"播放/继续"重播当前曲目；主动"下一首"正常推进。
4. 自动续播竞争：轮询已捕获 idle 上升沿（origin=auto）但停止先完成
   （_last_idle=True）→ 锁内守卫拦截，不启动下一首、无额外 loadfile；
   对照：自然播放结束（_last_idle=False）→ 正常自动切歌。
5. CommandBus / Runtime 派发：MUSIC_STOP → stop_current，不触发 clear_queue、
   不走电影暂停路由。

全部使用可控 Fake MPV，不依赖真实 mpv 进程；同步靠确定性状态构造，非 sleep。
"""
import pytest

pytestmark = pytest.mark.unit

from unittest.mock import MagicMock, patch

from app.core.command_bus import Command, CommandType
from app.core.state_store import StateNamespace
from app.music.controller import MusicController
from app.voice.intent import RuleBasedIntent


# ===== Fake MPV（可控 idle 状态与调用记录） =====

class FakeMpv:
    """不发真声音的 mpv 替身（协议满足 MpvController）。

    idle_active 可配置：get_property("idle-active") 返回设定值，
    用于构造"轮询观察到 idle"的时序。
    stop_result 可配置 mpv.stop() 返回值：True=协议成功 / False=错误响应
    / None=超时未确认（第六阶段三态语义）。
    """

    def __init__(self, idle_active=False, stop_result=True):
        self.calls = []
        self._idle_active = idle_active
        self._stop_result = stop_result
        self._properties = {}
        self.volume = 70

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


def _make_ctrl(tmp_path, fake, db_name="stop_semantics.db"):
    cfg = {
        "database": str(tmp_path / db_name),
        "scan_dirs": [],
        "extensions": [".flac"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_stop_semantics",
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


def _loadfile_paths(fake):
    return [c[1] for c in fake.calls if c[0] == "loadfile"]


# ===== Intent 解析 =====

class TestIntentMapping:
    def _match(self, text):
        intent = RuleBasedIntent.match(text)
        assert intent is not None, f"{text!r} 未命中任何意图"
        return intent.intent_type

    @pytest.mark.parametrize("phrase", [
        "停止音乐", "停掉音乐", "把音乐停了", "音乐停了",
        "音乐停掉", "停止放歌", "把歌停了", "歌别放了",
    ])
    def test_music_stop_phrases(self, phrase):
        assert self._match(phrase) == CommandType.MUSIC_STOP

    @pytest.mark.parametrize("phrase", ["停止说话", "别说了", "闭嘴", "停止播放"])
    def test_stop_tts_phrases(self, phrase):
        assert self._match(phrase) == "system.stop_tts"

    def test_pause_still_routes_to_music_pause(self):
        # 既有语义不得回归：暂停音乐 ≠ 停止
        assert self._match("暂停音乐") == CommandType.MUSIC_PAUSE
        assert self._match("别放了") == CommandType.MUSIC_PAUSE

    def test_clear_queue_unchanged(self):
        assert self._match("清空队列") == CommandType.MUSIC_CLEAR_QUEUE


# ===== Controller stop_current() =====

class TestStopCurrent:
    def test_stop_current_stops_mpv_and_clears_current(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False  # 模拟正处于播放态
        ctrl.stop_current()
        assert ("stop",) in fake.calls
        assert ctrl._current_song is None
        assert ctrl._last_idle is True
        assert ctrl.state_store.get(StateNamespace.MUSIC, "playing", True) is False

    def test_stop_current_preserves_queue_and_pointer(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()  # 指针指向 A（index 0）
        ctrl._current_song = ctrl.queue.get_current()
        current_before = ctrl.queue.get_current()["id"]
        ctrl.stop_current()
        assert not ctrl.queue.is_empty()
        assert ctrl.queue.get_current()["id"] == current_before

    def test_stop_current_invalidates_highlight(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl._highlight_gen = 5
        ctrl._highlight_song_id = 9
        ctrl.stop_current()
        assert ctrl._highlight_gen == 6
        assert ctrl._highlight_song_id is None

    def test_stop_current_empty_queue_no_error(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.stop_current()  # 不应抛
        assert ctrl._current_song is None

    def test_stop_current_logs_failure_when_mpv_stop_raises(self, tmp_path):
        """mpv.stop() 抛异常（如 IPC 未连接）→ 记录带异常信息的错误日志，
        且不得伪装停止成功：_current_song/_last_idle/playing 状态保留现状。"""
        fake = FakeMpv()

        def boom():
            raise ConnectionError("mpv IPC 未连接")
        fake.stop = boom
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False  # 播放中基线
        ctrl._update_state(playing=True)

        # controller.logger 为自定义 stdout 包装（非标准 logging），用 patch 断言调用
        with patch("app.music.controller.logger") as mock_logger:
            ctrl.stop_current()
        mock_logger.error.assert_called_once()
        msg = mock_logger.error.call_args[0][0]
        assert "mpv.stop() 失败" in msg
        assert "mpv IPC 未连接" in msg
        # 失败 → 不宣称停止成功：当前曲目、idle 基线、播放状态均保留
        assert ctrl._current_song is not None
        assert ctrl._current_song["id"] == 1
        assert ctrl._last_idle is False
        assert ctrl.state_store.get(StateNamespace.MUSIC, "playing", None) is True
        # 队列内容与指针不变
        assert not ctrl.queue.is_empty()
        assert ctrl.queue.get_current()["id"] == 1

    def test_play_after_stop_replays_current_song(self, tmp_path):
        """停止后主动"播放" → play() 的 idle 重载路径重播当前曲目（不推进指针）。"""
        fake = FakeMpv(idle_active=True)  # 停止后 mpv idle
        ctrl = _make_ctrl(tmp_path, fake)
        a = _local_song(tmp_path, 1, "A")
        ctrl.queue.add(a)
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()  # 指针 → A
        ctrl.stop_current()
        ctrl.play()
        loads = _loadfile_paths(fake)
        assert len(loads) == 1
        assert loads[0] == a["path"]
        assert ctrl.queue.get_current()["id"] == 1  # 指针未推进（仍 A）

    def test_next_after_stop_advances(self, tmp_path):
        """停止后主动"下一首" → 正常推进并起播下一首。"""
        fake = FakeMpv(idle_active=True)
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(b)
        ctrl.queue.next()  # 指针 → A
        ctrl.stop_current()
        ctrl.next()
        assert ctrl.queue.get_current()["id"] == 2
        assert b["path"] in _loadfile_paths(fake)


# ===== 自动续播竞争守卫 =====

class TestAutoNextGuard:
    def test_stopped_idle_does_not_start_next(self, tmp_path):
        """真实时序：stop_current 完成（_last_idle=True）→ 轮询/auto 路径的
        next() 被锁内守卫拦截：指针不推进、无 loadfile。"""
        fake = FakeMpv(idle_active=True)  # mpv 已 idle（停止后）
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl.stop_current()
        loads_before = _loadfile_paths(fake)
        # 模拟轮询捕获上升沿后执行自动切歌（origin=auto，实时 idle 复核通过）
        with ctrl._origin_ctx("auto"):
            ctrl.next()
        assert ctrl.queue.get_current()["id"] == 1  # 指针未推进（仍 A）
        assert _loadfile_paths(fake) == loads_before  # 无额外 loadfile
        assert ctrl._last_idle is True

    def test_natural_eof_still_auto_advances(self, tmp_path):
        """对照：自然播放结束（无人停止，_last_idle=False）→ 自动切歌仍正常推进。"""
        fake = FakeMpv(idle_active=True)  # 播放结束 mpv idle
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        b = _local_song(tmp_path, 2, "B")
        ctrl.queue.add(b)
        ctrl.queue.next()
        ctrl._current_song = ctrl.queue.get_current()
        ctrl._last_idle = False  # 播放中基线
        with ctrl._origin_ctx("auto"):
            ctrl.next()
        assert ctrl.queue.get_current()["id"] == 2
        assert b["path"] in _loadfile_paths(fake)

    def test_user_next_unaffected_by_guard(self, tmp_path):
        """用户主动"下一首"（origin=user）不受守卫影响，即使 _last_idle=True。"""
        fake = FakeMpv(idle_active=True)
        ctrl = _make_ctrl(tmp_path, fake)
        ctrl.queue.add(_local_song(tmp_path, 1, "A"))
        ctrl.queue.add(_local_song(tmp_path, 2, "B"))
        ctrl.queue.next()
        ctrl._last_idle = True
        ctrl.next()
        assert ctrl.queue.get_current()["id"] == 2


# ===== CommandBus / Runtime 派发 =====

class TestDispatch:
    def test_command_bus_routes_to_stop_current_not_clear_queue(self, tmp_path):
        fake = FakeMpv()
        ctrl = _make_ctrl(tmp_path, fake)
        with patch.object(ctrl, "stop_current") as sc, \
                patch.object(ctrl, "clear_queue") as cq:
            ctrl.command_bus.execute(Command(
                command_type=CommandType.MUSIC_STOP,
                params={},
                source="test",
            ))
            sc.assert_called_once()
            cq.assert_not_called()

    def test_runtime_dispatches_music_stop_directly(self):
        """VoiceRuntime：MUSIC_STOP 直接走 CommandBus（不经电影暂停路由）。"""
        from app.voice.runtime import VoiceRuntime
        from app.voice.intent import Intent
        rt = VoiceRuntime.__new__(VoiceRuntime)
        rt.command_bus = MagicMock()
        rt.event_bus = MagicMock()
        rt.tts_provider = None
        rt.speech_queue_provider = None
        intent = Intent(CommandType.MUSIC_STOP, confidence=0.99, params={})
        rt._execute_intent(intent, "停止音乐")
        rt.command_bus.execute.assert_called_once()
        cmd = rt.command_bus.execute.call_args[0][0]
        assert cmd.command_type == CommandType.MUSIC_STOP
        assert cmd.command_type != CommandType.MOVIE_PAUSE
