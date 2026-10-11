"""一键连招模块测试（一期）。

全部使用 fake key_sender / fake ongoing session，不依赖真实键盘、
不发送真实按键、不需要管理员权限。
"""
from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.combo.config import ComboConfig  # noqa: E402
from app.combo.executor import ComboExecutor  # noqa: E402
from app.combo.manager import ComboManager, EXEC_GRACE_S  # noqa: E402
from app.combo.models import Combo, ComboStep  # noqa: E402


# ---------- 配置 ----------

def test_config_load_yaml(tmp_path):
    cfg = ComboConfig.load(str(tmp_path / "combos.yaml"))
    assert len(cfg.champions) == 2
    by_id = {c.champion_id: c for c in cfg.champions}
    assert set(by_id) == {64, 412}
    lee = by_id[64]
    assert lee.name == "盲僧"
    assert lee.find_by_trigger("3").note == "摸眼位移"
    assert [s.key for s in lee.find_by_trigger("3").sequence] == ["3", "w", "w"]
    assert lee.find_by_trigger("A").note == "4wd（插眼-摸眼-闪现补距离）"  # 大小写不敏感
    assert by_id[412].find_by_trigger("a").note == "超远丢灯笼"


def test_config_missing_file_creates_default(tmp_path):
    path = str(tmp_path / "nope" / "combos.yaml")
    cfg = ComboConfig.load(path)
    assert os.path.exists(path)  # 自动创建默认文件
    assert len(cfg.champions) == 2


def test_config_bad_yaml_falls_back(tmp_path):
    bad = tmp_path / "combos.yaml"
    bad.write_text(":::: not yaml [[[", encoding="utf-8")
    cfg = ComboConfig.load(str(bad))
    assert isinstance(cfg, ComboConfig)  # 解析失败不崩


def test_config_bad_entries_skipped(tmp_path):
    p = tmp_path / "combos.yaml"
    p.write_text(
        "champions:\n"
        "  坏英雄:\n"
        "    champion_id: 0\n"
        "  正常:\n"
        "    champion_id: 1\n"
        "    combos:\n"
        "      - trigger: q\n"
        "        sequence:\n"
        "          - { key: q, delay_after_ms: 10 }\n",
        encoding="utf-8",
    )
    cfg = ComboConfig.load(str(p))
    assert len(cfg.champions) == 1
    assert cfg.champions[0].champion_id == 1


# ---------- 执行引擎 ----------

class FakeSender:
    def __init__(self):
        self.keys = []
        self.delays = []

    def __call__(self, key):
        self.keys.append(key)


def test_executor_sequence_order_and_delay(tmp_path, monkeypatch):
    sender = FakeSender()
    exec_ = ComboExecutor(key_sender=sender)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    combo = Combo(trigger="a", note="4wwd", sequence=[
        ComboStep("4", 50), ComboStep("w", 0), ComboStep("w", 50), ComboStep("d", 50),
    ])
    assert exec_.run(combo) is True
    assert sender.keys == ["4", "w", "w", "d"]
    assert slept == [0.05, 0.05, 0.05]  # 0ms 步骤不 sleep


def test_executor_rejects_concurrent_run(tmp_path, monkeypatch):
    sender = FakeSender()
    exec_ = ComboExecutor(key_sender=sender)
    # 执行中用 barrier 卡住
    import threading
    barrier = threading.Event()

    def slow_send(key):
        sender.keys.append(key)
        barrier.wait(2)

    exec_ = ComboExecutor(key_sender=slow_send)
    combo = Combo(trigger="a", sequence=[ComboStep("d", 50), ComboStep("w", 50)])
    t = threading.Thread(target=exec_.run, args=(combo,))
    t.start()
    time.sleep(0.05)
    assert exec_.is_executing is True
    assert exec_.run(combo) is False  # 执行中拒绝并发
    barrier.set()
    t.join(timeout=2)
    assert exec_.is_executing is False


# ---------- Manager 全流程 ----------

def make_session(self_champion_id):
    """构造 fake ongoing session（CurrentGameSession 最小形态）。"""
    self_player = SimpleNamespace(is_self=True, champion_id=self_champion_id)
    other = SimpleNamespace(is_self=False, champion_id=1)
    return SimpleNamespace(my_team=[self_player, other])


class FakeOngoing:
    def __init__(self, session):
        self._session = session

    def get_session(self):
        return self._session


def make_manager(tmp_path, key_sender=None):
    m = ComboManager(
        config_path=str(tmp_path / "combos.yaml"),
        key_sender=key_sender,
        ongoing_service=FakeOngoing(make_session(64)),
        usage_path=str(tmp_path / "usage.json"),
    )
    return m


def test_manager_disabled_ignores_key(tmp_path):
    m = make_manager(tmp_path, FakeSender())
    assert m.is_enabled() is False
    m._handle_key("3")
    assert m._executor.is_executing is False
    assert m._executor._key_sender.keys == []


def test_manager_no_session_ignores_key(tmp_path):
    m = make_manager(tmp_path, FakeSender())
    m.set_window_gate(False)  # 隔离前台门控，只测"无 session 分支"
    m.set_enabled(True)
    m._ongoing_service._session = None  # 对局结束
    m._handle_key("3")
    assert m._executor._key_sender.keys == []


def test_manager_trigger_not_matched_ignored(tmp_path):
    m = make_manager(tmp_path, FakeSender())
    m.set_window_gate(False)  # 隔离前台门控，只测"trigger 不匹配分支"
    m.set_enabled(True)
    m._handle_key("q")  # 盲僧无 q 连招
    assert m._executor._key_sender.keys == []


def test_manager_full_flow_lee_sin(tmp_path):
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_window_gate(False)  # 隔离前台门控，只测主链路
    m.set_enabled(True)
    assert m.current_champion_name() == "盲僧"
    m._handle_key("3")
    assert sender.keys == ["3", "w", "w"]


def test_manager_full_flow_thresh(tmp_path):
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_window_gate(False)  # 隔离前台门控，只测主链路
    m.set_enabled(True)
    m._ongoing_service._session = make_session(412)
    m._handle_key("a")
    assert sender.keys == ["d", "w", "q"]


def test_manager_grace_window_blocks_self_trigger(tmp_path):
    """关键：执行后 grace 窗口内按键被忽略，程序自己发的键不会再次触发。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_window_gate(False)  # 隔离前台门控，只测 grace 窗口
    m.set_enabled(True)
    m._handle_key("3")  # 触发连招 3→W→W（W 非 trigger，发的键无风险）
    assert sender.keys == ["3", "w", "w"]
    assert time.time() < m._ignore_until  # grace 窗口已设置
    m._handle_key("3")  # 窗口内再按，应被忽略
    assert len(sender.keys) == 3


def test_manager_no_self_loop_after_grace(tmp_path, monkeypatch):
    """grace 过后按键恢复响应（不是永久屏蔽）。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_window_gate(False)  # 隔离前台门控，只测 grace 恢复
    m.set_enabled(True)
    m._handle_key("4")  # 4→W→W
    assert sender.keys == ["4", "w", "w"]
    m._ignore_until = time.time() - 0.1  # 模拟窗口已过
    m._handle_key("4")
    assert sender.keys == ["4", "w", "w", "4", "w", "w"]


def test_manager_usage_bump(tmp_path):
    m = make_manager(tmp_path, FakeSender())
    assert m.get_usage(64) == 0
    m.bump_usage(64)
    m.bump_usage(64)
    assert m.get_usage(64) == 2
    # 重新加载（持久化）
    m2 = make_manager(tmp_path, FakeSender())
    assert m2.get_usage(64) == 2


def test_manager_champion_clear_on_game_end(tmp_path):
    m = make_manager(tmp_path, FakeSender())
    m.set_window_gate(False)  # 隔离前台门控，只测"对局结束清空"分支
    m.set_enabled(True)
    assert m.current_champion_name() == "盲僧"
    m._ongoing_service._session = None
    assert m.current_champion_name() is None
    m._handle_key("3")
    assert m._executor._key_sender.keys == []


def test_listener_hook_lifecycle(tmp_path):
    """start → stop → hook 精确取消、_remove_hook 清空、可再次 start。

    用 keyboard 库内部状态做确定性断言：
    - hook() 注册时 _hooks 以 callback 和 remove_ 为 key（__init__.py:467）
    - unhook(remove_) 执行 remove_ → 从 _hooks 删除 callback 与 remove_ 两个 key
      （__init__.py:462-467），并从 _listener.handlers 移除原 callback
    """
    import keyboard
    m = make_manager(tmp_path, FakeSender())
    cb = m._listener._callback

    m._listener.start()
    assert m._listener._hooked is True
    assert m._listener._remove_hook is not None
    assert cb in keyboard._listener.handlers
    assert cb in keyboard._hooks
    remove_fn = m._listener._remove_hook
    assert remove_fn in keyboard._hooks

    m._listener.stop()
    assert m._listener._hooked is False
    assert m._listener._remove_hook is None
    assert cb not in keyboard._listener.handlers  # 原 callback 已从 handlers 移除
    assert cb not in keyboard._hooks              # _hooks 条目已清理
    assert remove_fn not in keyboard._hooks

    # 其他 hook 不受影响：注册一个临时 hook 验证 unhook 不是全局清除
    sentinel = lambda e: None
    sentinel_remove = keyboard.hook(sentinel)
    m._listener.start()
    assert sentinel in keyboard._listener.handlers  # 别人的 hook 还在
    m._listener.stop()
    assert sentinel in keyboard._listener.handlers  # 我们 stop 后别人的 hook 仍存活
    keyboard.unhook(sentinel_remove)  # 清理测试哨兵
    assert sentinel not in keyboard._listener.handlers


# ---------- 前台窗口门控 ----------

def test_window_gate_lol_foreground_allows(tmp_path, monkeypatch):
    """前台是 LoL 游戏本体 → 放行触发。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_enabled(True)
    assert m.is_window_gate_enabled() is True  # 默认开
    monkeypatch.setattr("app.combo.manager.is_lol_foreground", lambda: True)
    m._handle_key("3")
    assert sender.keys == ["3", "w", "w"]


def test_window_gate_other_window_blocks(tmp_path, monkeypatch):
    """前台是其他窗口 → 拦截。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_enabled(True)
    monkeypatch.setattr("app.combo.manager.is_lol_foreground", lambda: False)
    m._handle_key("3")
    assert sender.keys == []


def test_window_gate_failure_allows_with_log(tmp_path, monkeypatch, capsys):
    """前台检测失败 → 放行 + 日志 [Combo] 前台检测失败，已放行。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_enabled(True)

    def boom():
        raise OSError("foreground query failed")

    monkeypatch.setattr("app.combo.manager.is_lol_foreground", boom)
    m._handle_key("3")
    assert sender.keys == ["3", "w", "w"]  # 失败放行
    assert "前台检测失败，已放行" in capsys.readouterr().out


def test_window_gate_disabled_ignores_foreground(tmp_path, monkeypatch):
    """开关关闭 → 不查前台，照常触发。"""
    sender = FakeSender()
    m = make_manager(tmp_path, sender)
    m.set_enabled(True)
    m.set_window_gate(False)
    monkeypatch.setattr("app.combo.manager.is_lol_foreground", lambda: False)
    m._handle_key("3")
    assert sender.keys == ["3", "w", "w"]
