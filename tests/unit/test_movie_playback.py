# -*- coding: utf-8 -*-
"""电影播放链状态契约单元测试（第七阶段）。

覆盖：
- stop() 三态（True/False/None/异常/MPV 未运行）状态处理；
- 自动连播回归（停止后不连播 / 自然播完连播 / 末集不循环）；
- 音乐/电影互斥（电影 play 暂停音乐 / 音乐事件暂停电影）。

全部使用 FakeMpv 替身：不启动真实 MPV、不创建 QApplication、不启动崩溃恢复线程。
get_state_store / get_event_bus / get_command_bus / get_config 的 patch 通过 ExitStack
fixture 存活到用例结束（_write_state 等运行时查找必须在用例生命周期内命中替身）。
"""
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from app.core.command_bus import CommandBus
from app.core.event_bus import Event, EventBus, EventType
from app.core.state_store import StateNamespace

# FakeMpv.loadfile 默认成功响应的哨兵（与"显式传 None=未确认"区分）
_DEFAULT_OK = {"error": "success"}


class FakeMpv:
    """不发真声音的电影 mpv 替身（协议满足 MpvController 的受测子集）。

    idle_active 可配：get_idle_active() 返回设定值，用于构造"播完 idle 上升沿"时序。
    stop_result 可配 mpv.stop() 返回值：True=协议成功 / False=错误响应 / None=超时未确认。
    running 可配 is_running()：False 表示进程未运行（stop 的未运行分支）。
    loadfile_result 可配 mpv.loadfile() 返回值（默认 success 响应，与真实接口"返回
    _send_command 结果"的契约一致）：{"error": "success"}=被接受 / None=未确认 /
    {"error": "..."}=协议层失败。loadfile_exc 非 None 时 loadfile 直接抛该异常。
    """

    def __init__(self, idle_active=False, stop_result=True, running=True,
                 loadfile_result=_DEFAULT_OK, loadfile_exc=None):
        self.calls = []
        self._idle_active = idle_active
        self._stop_result = stop_result
        self._running = running
        self._properties = {}
        self._loadfile_result = loadfile_result
        self._loadfile_exc = loadfile_exc

    def is_running(self):
        return self._running

    def get_idle_active(self):
        self.calls.append(("get_idle_active",))
        return self._idle_active

    def stop(self):
        self.calls.append(("stop",))
        return self._stop_result

    def stop_process(self):
        self.calls.append(("stop_process",))

    def start(self, taskkill=False, wid=None, pipe_timeout=None, prop_timeout=None):
        self.calls.append(("start", taskkill, wid))
        return True

    def loadfile(self, url, mode="replace"):
        self.calls.append(("loadfile", url, mode))
        if self._loadfile_exc is not None:
            raise self._loadfile_exc
        return self._loadfile_result

    def set_property(self, name, value):
        self.calls.append(("set_property", name, value))
        self._properties[name] = value

    def pause(self):
        self.calls.append(("pause",))

    def play(self):
        self.calls.append(("play",))

    def seek(self, seconds, mode="relative"):
        self.calls.append(("seek", seconds, mode))


class FakeMusic:
    """音乐控制器替身（仅记录互斥调用）。"""

    def __init__(self):
        self.calls = []

    def pause(self):
        self.calls.append(("pause",))


class FakeStore:
    """StateStore 替身：记录 (namespace, key) -> value 写入。"""

    def __init__(self):
        self.data = {}

    def set(self, namespace, key, value):
        self.data[(namespace, key)] = value


# 多集条目：play_url 用 "#" 拆集，每集 "名$url"（episode_eps 读 play_url）
ITEM_MULTI = {
    "vod_name": "测试剧",
    "play_url": "第1集$http://example.com/1#第2集$http://example.com/2",
}
ITEM_SINGLE = {
    "vod_name": "测试片",
    "play_url": "正片$http://example.com/movie",
}


@pytest.fixture
def env():
    """ExitStack：patch 存活到用例结束（get_state_store 等运行时时查找需命中替身）。"""
    with ExitStack() as stack:
        yield stack


def _make_config(tmp_path):
    return {
        "music.mpv_path": "mpv",
        "movie.mpv_ipc_pipe": r"\\.\pipe\test_movie_mpv",
        "movie.watch_later_dir": str(tmp_path / "watch_later"),
        "app.data_dir": str(tmp_path / "data"),
        "movie.embed": False,
        "movie.auto_next": True,
    }


def make_controller(env, fake, config, music=None, auto_next=True):
    """构造 MovieController：注入 FakeMpv + 全新 EventBus/CommandBus/FakeStore。

    patch 全部挂在 env（ExitStack）上，随用例生命周期结束而恢复——构造副作用
    （订阅/注册）落在全新 bus 实例上，运行时 _write_state 命中 FakeStore。
    """
    fake_store = FakeStore()
    fake_bus = EventBus()
    cfg = dict(config)
    cfg["movie.auto_next"] = auto_next
    env.enter_context(patch("app.movie.movie_controller.MpvController", return_value=fake))
    env.enter_context(patch("app.movie.movie_controller.get_config", return_value=cfg))
    env.enter_context(patch("app.movie.movie_controller.get_event_bus", return_value=fake_bus))
    env.enter_context(patch("app.core.command_bus.get_command_bus", return_value=CommandBus()))
    env.enter_context(patch("app.movie.movie_controller.get_state_store", return_value=fake_store))
    from app.movie.movie_controller import MovieController
    mc = MovieController(music=music)
    return mc, fake_store, fake_bus


def _prime_playing(mc, item, index=0, last_idle=False):
    """把控制器置于"正在播放某集"的既有上下文（绕过网络，直接播种状态）。"""
    mc._current = item
    mc._current_index = index
    mc._last_idle = last_idle


# ===== A. stop() 状态契约 =====

def test_stop_success_clears_context_and_writes_state(tmp_path, env):
    fake = FakeMpv(stop_result=True, running=True, idle_active=False)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    mc.stop()

    assert ("stop",) in fake.calls
    assert mc._current is None
    assert mc._current_index is None
    assert mc._last_idle is True
    assert mc._stopping is False
    assert store.data[(StateNamespace.MOVIE, "playing")] is False
    assert store.data[(StateNamespace.MOVIE, "paused")] is False


def test_stop_error_preserves_context_and_state(tmp_path, env):
    fake = FakeMpv(stop_result=False, running=True)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=1, last_idle=False)
    store.data[(StateNamespace.MOVIE, "playing")] = True  # 播放权仍在

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.stop()

    # 上下文与播放状态保留
    assert mc._current is ITEM_MULTI
    assert mc._current_index == 1
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    # _last_idle 钉死，防旧基线误触发自动连播
    assert mc._last_idle is True
    # 无"停止成功"的 StateStore 写入（paused 未被写）
    assert (StateNamespace.MOVIE, "paused") not in store.data
    assert mc._stopping is False
    assert ("stop",) in fake.calls
    mock_logger.error.assert_called_once()
    assert "返回错误响应" in mock_logger.error.call_args[0][0]


def test_stop_unconfirmed_preserves_context_and_state(tmp_path, env):
    fake = FakeMpv(stop_result=None, running=True)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)
    store.data[(StateNamespace.MOVIE, "playing")] = True

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.stop()

    assert mc._current is ITEM_MULTI
    assert mc._current_index == 0
    assert mc._last_idle is True
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    assert mc._stopping is False
    mock_logger.error.assert_called_once()
    assert "结果未确认" in mock_logger.error.call_args[0][0]


def test_stop_exception_preserves_context_and_state(tmp_path, env):
    fake = FakeMpv(stop_result=True, running=True)

    def _boom():
        raise RuntimeError("mpv IPC 未连接")

    fake.stop = _boom
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)
    store.data[(StateNamespace.MOVIE, "playing")] = True

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.stop()

    assert mc._current is ITEM_MULTI
    assert mc._current_index == 0
    assert mc._last_idle is True
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    assert mc._stopping is False
    mock_logger.error.assert_called_once()
    msg = mock_logger.error.call_args[0][0]
    assert "失败" in msg and "mpv IPC 未连接" in msg


def test_stop_when_mpv_not_running_cleans_up(tmp_path, env):
    fake = FakeMpv(running=False)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    mc.stop()

    # 无进程可停：不调用 stop()，按既有语义幂等清理
    assert ("stop",) not in fake.calls
    assert mc._current is None
    assert mc._current_index is None
    assert mc._last_idle is True
    assert store.data[(StateNamespace.MOVIE, "playing")] is False
    assert mc._stopping is False


# ===== B. 自动连播回归保护 =====

def test_stop_success_then_tick_does_not_advance(tmp_path, env):
    fake = FakeMpv(stop_result=True, running=True, idle_active=True)
    mc, _, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    mc.stop()
    mc.tick()

    # _current 已清 → tick 直接返回，绝不 loadfile 下一集
    assert not any(c[0] == "loadfile" for c in fake.calls)
    assert mc._current is None


def test_natural_end_advances_to_next_episode(tmp_path, env):
    fake = FakeMpv(idle_active=True, running=True)  # idle=True：该集已播完
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    mc.tick()

    # idle 上升沿 → 自动切下一集
    loads = [c for c in fake.calls if c[0] == "loadfile"]
    assert len(loads) == 1
    assert loads[0][1] == "http://example.com/2"
    assert mc._current_index == 1
    assert mc._current is ITEM_MULTI
    assert mc._last_idle is True


def test_last_episode_end_stops_without_loop(tmp_path, env):
    fake = FakeMpv(idle_active=True, running=True)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_SINGLE, index=0, last_idle=False)

    mc.tick()

    # 末集播完：不循环（无 loadfile），清上下文并写停止状态
    assert not any(c[0] == "loadfile" for c in fake.calls)
    assert mc._current is None
    assert mc._current_index is None
    assert store.data[(StateNamespace.MOVIE, "playing")] is False


# ===== C. 音乐/电影互斥 =====

def test_play_pauses_music(tmp_path, env):
    fake = FakeMpv(running=True)  # 已运行 → play 不启动进程
    music = FakeMusic()
    mc, _, _ = make_controller(env, fake, _make_config(tmp_path), music=music)

    ok = mc.play(ITEM_MULTI)

    assert ok is True
    assert ("pause",) in music.calls          # 电影开始播放 → 先暂停音乐
    assert ("loadfile", "http://example.com/1", "replace") in fake.calls
    assert mc._current is ITEM_MULTI
    assert mc._current_index == 0


def test_music_event_pauses_movie(tmp_path, env):
    fake = FakeMpv(running=True)
    mc, _, bus = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    # 音乐换歌 → MUSIC_TRACK_CHANGED（构造时已订阅到注入的新 EventBus）
    bus.publish(Event(event_type=EventType.MUSIC_TRACK_CHANGED,
                      data={"song_id": 1}, source="test"))

    assert ("pause",) in fake.calls
    assert mc._pausing is False
    assert mc._current is ITEM_MULTI           # 暂停保留播放权与上下文


def test_music_events_pause_movie_each_time(tmp_path, env):
    """每个音乐播放事件各触发一次电影暂停（mpv.pause 幂等，无状态错乱）。"""
    fake = FakeMpv(running=True)
    mc, _, bus = make_controller(env, fake, _make_config(tmp_path))
    _prime_playing(mc, ITEM_MULTI, index=0, last_idle=False)

    bus.publish(Event(event_type=EventType.MUSIC_TRACK_CHANGED, data={}, source="test"))
    bus.publish(Event(event_type=EventType.MUSIC_PLAY, data={}, source="test"))

    # 两次事件各触发一次暂停；守卫复位、上下文保留
    assert fake.calls.count(("pause",)) == 2
    assert mc._pausing is False
    assert mc._current is ITEM_MULTI


# ===== 构造隔离 =====

def test_constructor_subscribes_only_isolated_bus(tmp_path, env):
    """构造订阅落在注入的全新 EventBus 上，全局单例订阅表不被污染。"""
    from app.core.command_bus import get_command_bus as real_get_command_bus
    from app.core.event_bus import get_event_bus as real_get_event_bus

    real_bus = real_get_event_bus()
    before = list(real_bus._subscribers.get(EventType.MUSIC_TRACK_CHANGED, []))

    fake = FakeMpv(running=True)
    mc, _, isolated_bus = make_controller(env, fake, _make_config(tmp_path))

    # 订阅注册在隔离实例上（MUSIC_TRACK_CHANGED → _on_music_playing）
    assert any(getattr(h, "__name__", "") == "_on_music_playing"
               for h in isolated_bus._subscribers.get(EventType.MUSIC_TRACK_CHANGED, []))
    # 全局单例无新增订阅
    after = list(real_bus._subscribers.get(EventType.MUSIC_TRACK_CHANGED, []))
    assert after == before
    assert mc._subscribed is True


# ===== D. play() loadfile 失败不伪造播放成功 =====

def test_play_loadfile_error_response_does_not_pretend_success(tmp_path, env):
    """协议层明确失败响应（error != success）→ 不写入失败影片、不写 playing=True。"""
    fake = FakeMpv(running=True, loadfile_result={"error": "file not found"})
    music = FakeMusic()
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path), music=music)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        ok = mc.play(ITEM_MULTI)

    assert ok is False
    assert mc._current is None
    assert mc._current_index is None
    assert (StateNamespace.MOVIE, "playing") not in store.data
    assert ("loadfile", "http://example.com/1", "replace") in fake.calls
    assert ("pause",) in music.calls      # 互斥先行行为保留：音乐已暂停（既有语义）
    mock_logger.error.assert_called_once()
    assert "失败" in mock_logger.error.call_args[0][0]


def test_play_loadfile_unconfirmed_does_not_pretend_success(tmp_path, env):
    """命令超时/无响应（resp=None，结果未确认）→ 不伪造播放成功。"""
    fake = FakeMpv(running=True, loadfile_result=None)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))

    with patch("app.movie.movie_controller.logger") as mock_logger:
        ok = mc.play(ITEM_MULTI)

    assert ok is False
    assert mc._current is None
    assert (StateNamespace.MOVIE, "playing") not in store.data
    mock_logger.error.assert_called_once()
    assert "未确认" in mock_logger.error.call_args[0][0]


def test_play_loadfile_exception_does_not_pretend_success(tmp_path, env):
    """连接/命令调用抛异常 → 电影状态不被伪造为成功。"""
    fake = FakeMpv(running=True,
                   loadfile_exc=ConnectionError("mpv IPC 未连接"))
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))

    with patch("app.movie.movie_controller.logger") as mock_logger:
        ok = mc.play(ITEM_MULTI)

    assert ok is False
    assert mc._current is None
    assert (StateNamespace.MOVIE, "playing") not in store.data
    mock_logger.error.assert_called_once()
    assert "异常" in mock_logger.error.call_args[0][0]


def test_play_loadfile_success_writes_playing_state(tmp_path, env):
    """成功响应（命令被接受）→ 按原有语义更新 _current 与 playing=True。"""
    fake = FakeMpv(running=True, loadfile_result={"error": "success"})
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))

    ok = mc.play(ITEM_SINGLE)

    assert ok is True
    assert mc._current is ITEM_SINGLE
    assert mc._current_index == 0
    assert mc._last_idle is False
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    assert store.data[(StateNamespace.MOVIE, "paused")] is False
    assert ("set_property", "pause", False) in fake.calls


# ===== E. 自动连播 _advance_episode 的 loadfile 失败不推进索引 =====

def _prime_idle_rising(mc, item, index=0):
    """播完上升沿基线：_current 已设、_last_idle=False、mpv idle=True。"""
    _prime_playing(mc, item, index=index, last_idle=False)


def test_advance_loadfile_error_keeps_current_index(tmp_path, env):
    """连播 loadfile 协议层失败 → 不把失败集推进为当前集、不写播放状态。"""
    fake = FakeMpv(running=True, idle_active=True,
                   loadfile_result={"error": "no such file"})
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.tick()

    assert mc._current is ITEM_MULTI
    assert mc._current_index == 0                 # 未推进到失败集
    assert (StateNamespace.MOVIE, "playing") not in store.data
    assert len([c for c in fake.calls if c[0] == "loadfile"]) == 1
    mock_logger.error.assert_called_once()
    assert "失败" in mock_logger.error.call_args[0][0]


def test_advance_loadfile_unconfirmed_keeps_current_index(tmp_path, env):
    """连播 loadfile 结果未确认（None）→ 不推进索引、不伪造成功。"""
    fake = FakeMpv(running=True, idle_active=True, loadfile_result=None)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.tick()

    assert mc._current_index == 0
    assert (StateNamespace.MOVIE, "playing") not in store.data
    mock_logger.error.assert_called_once()
    assert "未确认" in mock_logger.error.call_args[0][0]


def test_advance_loadfile_exception_keeps_current_index(tmp_path, env):
    """连播 loadfile 抛异常 → 不冒泡、不推进索引、不伪造成功。"""
    fake = FakeMpv(running=True, idle_active=True,
                   loadfile_exc=ConnectionError("mpv IPC 未连接"))
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        mc.tick()   # 不得向 UI 轮询冒泡

    assert mc._current_index == 0
    assert (StateNamespace.MOVIE, "playing") not in store.data
    mock_logger.error.assert_called_once()
    assert "异常" in mock_logger.error.call_args[0][0]


# ===== F. 崩溃恢复 _restart_worker 的 loadfile 失败不伪造恢复成功 =====

def _run_restart_worker(mc, item, idx, url, pos=None):
    """同步直调 _restart_worker（确定性；daemon 线程路径由 restart_current 兜底覆盖）。"""
    mc._restart_worker(item, idx, url, pos, wid=None)


def test_restart_worker_loadfile_error_does_not_pretend_restored(tmp_path, env):
    """恢复 loadfile 协议层失败 → 不写 playing=True、不重置连播基线。"""
    fake = FakeMpv(loadfile_result={"error": "no such file"})
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        _run_restart_worker(mc, ITEM_MULTI, 0, "http://example.com/1")

    assert mc._last_idle is False                 # 未重置为"已恢复播放"基线
    assert (StateNamespace.MOVIE, "playing") not in store.data
    assert mc._recover_pending is False           # 不遗留恢复中状态
    mock_logger.error.assert_called_once()
    assert "失败" in mock_logger.error.call_args[0][0]


def test_restart_worker_loadfile_unconfirmed_does_not_pretend_restored(tmp_path, env):
    """恢复 loadfile 结果未确认（None）→ 不伪造恢复成功。"""
    fake = FakeMpv(loadfile_result=None)
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        _run_restart_worker(mc, ITEM_MULTI, 0, "http://example.com/1")

    assert (StateNamespace.MOVIE, "playing") not in store.data
    assert mc._recover_pending is False
    mock_logger.error.assert_called_once()
    assert "未确认" in mock_logger.error.call_args[0][0]


def test_restart_worker_loadfile_exception_does_not_leave_pending(tmp_path, env):
    """恢复 loadfile 抛异常 → 不写成功状态，且不遗留恢复中状态。"""
    fake = FakeMpv(loadfile_exc=ConnectionError("mpv IPC 未连接"))
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    with patch("app.movie.movie_controller.logger") as mock_logger:
        _run_restart_worker(mc, ITEM_MULTI, 0, "http://example.com/1")

    assert (StateNamespace.MOVIE, "playing") not in store.data
    assert mc._recover_pending is False
    mock_logger.error.assert_called_once()
    assert "异常" in mock_logger.error.call_args[0][0]


def test_restart_worker_loadfile_success_restores(tmp_path, env):
    """恢复 loadfile 成功（命令被接受）→ 恢复播放状态与连播基线。"""
    fake = FakeMpv(loadfile_result={"error": "success"})
    mc, store, _ = make_controller(env, fake, _make_config(tmp_path))
    _prime_idle_rising(mc, ITEM_MULTI, index=0)

    _run_restart_worker(mc, ITEM_MULTI, 0, "http://example.com/1", pos=12.5)

    assert mc._last_idle is False
    assert store.data[(StateNamespace.MOVIE, "playing")] is True
    assert store.data[(StateNamespace.MOVIE, "paused")] is False
    assert ("seek", 12.5, "absolute") in fake.calls
    assert mc._recover_pending is False
