"""修 1（P0）：combo 钩子回调轻量化 + 看门狗自动重连。

覆盖：
- win_focus 不再输出 4 行诊断 print（940 行刷屏源）
- 前台检测 200ms 缓存（monotonic）
- 英雄解析 5s 缓存（monotonic）
- set_window_gate 清前台缓存
- watchdog：零回调超阈值 → 自动重连；近期有回调 → 静默
"""
from __future__ import annotations

import time

import pytest

from app.combo import win_focus
from app.combo.manager import ComboManager
from app.combo.listener import ComboListener
from app.combo.models import ChampionCombos


# ---------- win_focus：无诊断 print ----------

import pytest

pytestmark = pytest.mark.unit


def test_win_focus_no_diag_print(capsys):
    """正常路径不再打 hwnd/pid/proc_name/match 四行诊断（异常路径提示保留）。"""
    try:
        win_focus.is_lol_foreground()
    except OSError:
        pass  # 测试环境无前台窗口/进程查询失败属正常，异常路径提示允许存在
    out = capsys.readouterr().out
    assert "[Combo][focus] hwnd=0x" not in out   # L30 已删
    assert "[Combo][focus] pid=" not in out      # L36 已删
    assert "[Combo][focus] proc_name=" not in out  # L46 已删
    assert "[Combo][focus] match=" not in out    # L48 已删


# ---------- manager：前台检测 200ms 缓存 ----------

def test_foreground_cache_hit(monkeypatch):
    """200ms 内连续两次按键，前台检测只执行一次。"""
    calls = {"n": 0}

    def fake_fg():
        calls["n"] += 1
        return True

    monkeypatch.setattr("app.combo.manager.is_lol_foreground", fake_fg)
    m = ComboManager()
    m.set_enabled(True)
    m.set_window_gate(True)
    m._handle_key("q")
    m._handle_key("q")
    assert calls["n"] == 1


def test_foreground_cache_expiry(monkeypatch):
    """超过 200ms 后再次按键，前台检测重新执行。"""
    calls = {"n": 0}

    def fake_fg():
        calls["n"] += 1
        return True

    monkeypatch.setattr("app.combo.manager.is_lol_foreground", fake_fg)
    m = ComboManager()
    m.set_enabled(True)
    m.set_window_gate(True)
    m._handle_key("q")                 # miss → 1
    m._fg_cache_ts = time.monotonic() - 5.0   # 手动过期
    m._handle_key("q")                 # miss → 2
    assert calls["n"] == 2


def test_set_window_gate_clears_fg_cache(monkeypatch):
    """门控开关变化立即清前台缓存（即使值不变）。"""
    calls = {"n": 0}

    def fake_fg():
        calls["n"] += 1
        return True

    monkeypatch.setattr("app.combo.manager.is_lol_foreground", fake_fg)
    m = ComboManager()
    m.set_enabled(True)
    m.set_window_gate(True)
    m._handle_key("q")                 # miss → 1
    m.set_window_gate(True)            # 清缓存
    m._handle_key("q")                 # miss → 2
    assert calls["n"] == 2


# ---------- manager：英雄解析 5s 缓存 ----------

class _FakePlayer:
    is_self = True
    champion_id = 67


class _FakeSession:
    my_team = [_FakePlayer()]


class _FakeOngoing:
    def __init__(self):
        self.n = 0

    def get_session(self):
        self.n += 1
        return _FakeSession()


def _manager_with_champ(ongoing=None) -> ComboManager:
    m = ComboManager(ongoing_service=ongoing)
    cc = ChampionCombos(name="盲僧", champion_id=67, aliases=["q"])
    m.config.champions.append(cc)
    m.config.by_id[67] = cc
    m.set_enabled(True)
    m.set_window_gate(False)  # 关 gate，专注测英雄缓存
    return m


def test_champ_resolve_cache(monkeypatch):
    """同英雄 5s 内连续按键：轻读 cid（内存）每按键一次，find_by_id 组合解析只做一次。"""
    fake = _FakeOngoing()
    m = _manager_with_champ(fake)
    orig = m.config.find_by_id
    calls = {"n": 0}

    def spy(cid):
        calls["n"] += 1
        return orig(cid)

    monkeypatch.setattr(m.config, "find_by_id", spy)
    m._handle_key("q")
    first = calls["n"]
    assert first > 0                       # 首次按键解析了组合
    m._handle_key("q")
    assert calls["n"] == first             # 缓存命中：不再解析组合
    assert fake.n == 2                     # cid 轻读每按键一次（内存读，微秒级）


def test_champ_cache_cleared_on_game_end():
    """对局结束（get_session 空）→ 缓存立即清空，下一按键重新查且英雄名回 None。"""
    class _EmptySession:
        my_team = []

    class _FakeOngoing2:
        def __init__(self):
            self.n = 0
            self.phase = 0

        def get_session(self):
            self.n += 1
            if self.phase == 0:
                return _FakeSession()   # 第一局有英雄
            return _EmptySession()      # 对局结束

    fake = _FakeOngoing2()
    m = _manager_with_champ(fake)
    m._handle_key("q")                  # 英雄 67，缓存写入
    assert m.current_champion_name() == "盲僧"
    assert m._champ_cache is not None
    fake.phase = 1                      # 模拟对局结束
    m._handle_key("q")                  # 轻读 cid=None → 清空英雄 + 清缓存
    assert m.current_champion_name() is None
    assert m._champ_cache is None


# ---------- listener：watchdog ----------

def test_watchdog_reconnect(monkeypatch):
    """零回调超阈值 → unhook + 重新 hook；重连成功后计数清零（上限=连续失败）。"""
    import app.combo.listener as L

    hooked = []

    def fake_hook(cb):
        def rm():
            while hooked:
                hooked.pop()
            return True

        hooked.append(cb)
        return rm

    monkeypatch.setattr("keyboard.hook", fake_hook)
    monkeypatch.setattr("keyboard.unhook", lambda rm: rm())
    lst = ComboListener(lambda k: None)
    lst._WATCH_IDLE_S = 0.05   # 测试阈值：很快判定掉线
    lst._WATCH_INTERVAL = 0.02
    lst.start()
    assert lst._hooked
    lst._last_cb_ts = 0.0      # 模拟长时间无回调
    deadline = time.time() + 2.0
    while len(hooked) < 1 and time.time() < deadline:
        time.sleep(0.05)
    assert len(hooked) == 1    # 重连后新 hook 已注册
    assert lst._reconnects == 0  # 重连成功 → 计数清零（上限=连续失败）
    lst.stop()


def test_watchdog_quiet_no_reconnect(monkeypatch):
    """近期有回调（hook 存活）→ watchdog 静默，不重连。"""
    import app.combo.listener as L

    hooked = []

    def fake_hook(cb):
        def rm():
            while hooked:
                hooked.pop()
            return True

        hooked.append(cb)
        return rm

    monkeypatch.setattr("keyboard.hook", fake_hook)
    monkeypatch.setattr("keyboard.unhook", lambda rm: rm())
    lst = ComboListener(lambda k: None)
    lst._WATCH_IDLE_S = 60.0   # 阈值大
    lst._WATCH_INTERVAL = 0.02
    lst.start()
    lst._last_cb_ts = time.monotonic()   # 近期有回调
    time.sleep(0.15)
    assert lst._reconnects == 0
    lst.stop()


def test_watchdog_success_resets_counter(monkeypatch):
    """连续两次"掉线→重连成功"→ 计数清零，不累计（上限=连续失败语义）。

    注：fake_unhook 会清空 hooked（模拟取消注册），所以断言用 hook_calls 计数
    而非 hooked 累计数量。
    """
    import app.combo.listener as L

    hooked = []
    hook_calls = []

    def fake_hook(cb):
        def rm():
            while hooked:
                hooked.pop()
            return True

        hooked.append(cb)
        hook_calls.append(1)
        return rm

    monkeypatch.setattr("keyboard.hook", fake_hook)
    monkeypatch.setattr("keyboard.unhook", lambda rm: rm())
    lst = ComboListener(lambda k: None)
    lst._WATCH_IDLE_S = 0.05
    lst._WATCH_INTERVAL = 0.02
    lst.start()

    # 第一次掉线 → 重连成功
    lst._last_cb_ts = 0.0
    deadline = time.time() + 2.0
    while len(hook_calls) < 1 and time.time() < deadline:
        time.sleep(0.05)
    assert len(hook_calls) == 1
    assert len(hooked) == 1
    assert lst._reconnects == 0

    # 第二次掉线 → 重连成功，计数仍为 0（每次成功清零）
    lst._last_cb_ts = 0.0
    deadline = time.time() + 2.0
    while len(hook_calls) < 2 and time.time() < deadline:
        time.sleep(0.05)
    assert len(hook_calls) == 2
    assert len(hooked) == 1   # unhook 清空后重新注册 1 个
    assert lst._reconnects == 0
    lst.stop()


def test_watchdog_limit_is_consecutive_failures(monkeypatch):
    """hook 连续失败 3 次 → 停止自动重连（上限=连续失败）。"""
    import threading

    lst = ComboListener(lambda k: None)
    lst._WATCH_IDLE_S = 0.05
    lst._WATCH_INTERVAL = 0.02

    def fail_hook(cb):
        raise RuntimeError("hook 失败")

    monkeypatch.setattr("keyboard.hook", fail_hook)

    # 绕过 start（start 的 hook 必失败），手动模拟 watchdog 循环前置状态
    lst._started = True
    lst._hooked = True
    lst._last_cb_ts = 0.0
    lst._remove_hook = lambda: True
    lst._stop_watch = threading.Event()
    lst._watch_thread = threading.Thread(
        target=lst._watchdog, daemon=True, name="watchdog-test")
    lst._watch_thread.start()

    deadline = time.time() + 3.0
    while lst._reconnects < 3 and time.time() < deadline:
        time.sleep(0.05)
    assert lst._reconnects == 3
    # 达上限后 watchdog 在下次迭代 break 退出线程（轮询等待，避免竞态）
    deadline = time.time() + 2.0
    while lst._watch_thread.is_alive() and time.time() < deadline:
        time.sleep(0.05)
    assert not lst._watch_thread.is_alive()
    lst.stop()
