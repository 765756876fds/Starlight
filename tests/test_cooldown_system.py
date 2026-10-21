"""A2.1 冷却系统测试：数据层 / CooldownCalculator / CooldownManager(Tracker) / Intent / Speech 路径。

运行方式：
  pytest tests/test_cooldown_system.py -q
或无 pytest 时直接：
  py -3.11 tests/test_cooldown_system.py
"""
from __future__ import annotations

import os
import sys
import types

import pytest

pytestmark = pytest.mark.unit


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 历史环境缺以下第三方包（仅导入期使用），显式 stub，不安装依赖。
for _mod in ("requests", "urllib3", "yaml", "websocket"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
sys.modules["websocket"].create_connection = lambda *a, **k: None
sys.modules["websocket"].WebSocketConnectionClosedException = type(
    "WebSocketConnectionClosedException", (Exception,), {})
_ul = sys.modules["urllib3"]
_ul.disable_warnings = lambda *a, **k: None
_ul.exceptions = types.SimpleNamespace(
    InsecureRequestWarning=type("InsecureRequestWarning", (Exception,), {}))

from app.lol.cooldown.calculator import (
    CooldownCalculator,
    effective_ability_cooldown,
    effective_ultimate_cooldown,
    effective_summoner_cooldown,
)
from app.lol.cooldown.policy import ReminderPolicy
from app.lol.cooldown.context import CooldownContext
from app.lol.cooldown.manager import CooldownManager


class FakeScheduler:
    """不真正 sleep；记录回调，便于手动触发 ready/remind。"""

    def __init__(self):
        self.tasks = {}
        self.counter = 0

    def schedule_once(self, delay, cb, task_id=None):
        self.counter += 1
        tid = task_id or f"fake-{self.counter}"
        self.tasks[tid] = cb
        return tid

    def cancel_once(self, tid):
        self.tasks.pop(tid, None)

    def fire(self, prefix):
        for tid in list(self.tasks):
            if tid.startswith(prefix):
                self.tasks.pop(tid)()


# ---------------- 数据层 ----------------

def _manager():
    m = CooldownManager(on_event=lambda c, d: None,
                        speech_enqueue=lambda t, e: None,
                        scheduler=FakeScheduler())
    m.set_game_time(0.0)  # 同步游戏时钟：未同步时 start_reminder 会返回 None
    return m


def test_data_champion_slots_exist():
    m = _manager()
    champs = m._champions.all()
    assert len(champs) > 100, "应加载完整英雄表"
    cid = champs[0].cid
    for slot in ("Q", "W", "E", "R"):
        a = m._abilities.get_ability(cid, slot)
        assert a is not None, f"{cid} 缺 {slot}"


def test_data_multi_level_cooldown():
    m = _manager()
    cid = m._champions.all()[0].cid
    cd1 = m._abilities.get_cooldown_by_level(cid, "Q", 1)
    cd5 = m._abilities.get_cooldown_by_level(cid, "Q", 5)
    assert cd1 is not None
    # 多等级数组长度应为 6（1-6 级）
    a = m._abilities.get_ability(cid, "Q")
    assert len(a.cooldown_by_level) == 6


def test_data_summoner_spell_lookup():
    m = _manager()
    flash = m._spells.get_by_name("闪现")
    assert flash is not None and flash.cooldown > 0


def test_data_missing_no_crash():
    m = _manager()
    # 不存在英雄技能不崩溃
    assert m.query_cooldown("完全不存在的英雄名xyz", "Q")["base_cooldown"] is None
    # 不存在召唤师技能
    assert m._spells.get_by_name("不存在的技能xyz") is None


def test_data_version_exists():
    import json
    import pathlib
    p = pathlib.Path("data/lol_data/version.json")
    assert p.exists()
    v = json.loads(p.read_text(encoding="utf-8"))
    assert v.get("patch") or v.get("updated_at")


# ---------------- Calculator ----------------

def test_calc_zero_haste():
    assert effective_ability_cooldown(100.0, 0) == 100.0
    assert CooldownCalculator.for_ability(100.0, "Q", 0, 0) == 100.0


def test_calc_ability_haste():
    # 100 CDR = 100/(100+100) = 50%
    assert effective_ability_cooldown(100.0, 100) == 50.0


def test_calc_ultimate_haste():
    # R 的有效 haste = ability_haste + ultimate_haste（叠加）
    assert effective_ultimate_cooldown(100.0, 100, 0) == 50.0
    assert effective_ultimate_cooldown(100.0, 0, 100) == 50.0
    # 给 R 传 AH=100（无 UH）应生效（不是忽略 AH）
    assert CooldownCalculator.for_ability(100.0, "R", ability_haste=100, ultimate_haste=0) == 50.0
    # AH=100 + UH=100 = haste 200 -> 100*100/300 = 33.33
    assert CooldownCalculator.for_ability(100.0, "R", ability_haste=100, ultimate_haste=100) == 33.33


def test_calc_summoner_haste():
    assert effective_summoner_cooldown(300.0, 50) == 200.0


def test_calc_missing_and_partial():
    assert effective_ability_cooldown(None) is None
    assert effective_ability_cooldown(60.0, None) == 60.0
    assert effective_ability_cooldown(60.0, -10) == 60.0  # 负 haste 回退 0
    assert CooldownCalculator.for_ability(60.0, "Q", ability_haste="abc") == 60.0


# ---------------- ReminderPolicy ----------------

def test_policy_per_slot():
    p = ReminderPolicy()
    assert p.for_skill("ability", "Q") == 5.0
    assert p.for_skill("ability", "R") == 15.0
    assert p.for_skill("summoner_spell", "Flash") == 30.0


# ---------------- CooldownManager (Tracker) ----------------

def test_tracker_start_and_remind():
    spoken = []
    sched = FakeScheduler()
    m = CooldownManager(on_event=lambda c, d: None,
                        speech_enqueue=lambda t, e: spoken.append(t),
                        scheduler=sched)
    m.set_game_time(0.0)  # 同步游戏时钟
    champ = m._champions.all()[0].name_zh
    r = m.start_reminder(champ, "W", side="enemy")
    assert r is not None
    assert r.estimated_cooldown > 0
    # R 大招提醒阈值 15
    rr = m.start_reminder(champ, "R", side="enemy")
    assert rr.remind_before == 15.0
    # 触发 remind：推进游戏时钟到提醒点（新架构：无现实定时器，set_game_time 驱动）
    m.set_game_time(max(r.remind_at, rr.remind_at))
    assert spoken, "应通过 speech_enqueue 播报"


def test_tracker_slot_alias_ultimate():
    m = _manager()
    champ = m._champions.all()[0].name_zh
    # “大招” 应归一化为 R
    r = m.start_reminder(champ, "大招")
    assert r is not None and r.skill_type == "ability"


def test_tracker_duplicate_are_distinct():
    m = _manager()
    champ = m._champions.all()[0].name_zh
    a = m.start_reminder(champ, "Q")
    b = m.start_reminder(champ, "Q")
    assert a.id != b.id  # 每次创建独立计时


def test_tracker_cancel_and_cleanup():
    m = _manager()
    champ = m._champions.all()[0].name_zh
    r = m.start_reminder(champ, "W")
    assert m.cancel(r.id) is True
    assert m.get(r.id) is None


def test_tracker_game_reset_cancel_all():
    m = _manager()
    champ = m._champions.all()[0].name_zh
    m.start_reminder(champ, "Q")
    m.start_reminder(champ, "W")
    n = m.cancel_all()
    assert n == 2 and m.list_active() == []


def test_tracker_summoner_spell():
    m = _manager()
    r = m.start_reminder("任意英雄", "闪现", side="enemy")
    assert r is not None and r.skill_type == "summoner_spell"
    assert abs(r.estimated_cooldown - 300.0) < 1.0


def test_query_summoner_spell():
    m = _manager()
    res = m.query_cooldown(None, "闪现")
    assert res["kind"] == "summoner_spell" and res["base_cooldown"] == 300.0


def test_query_ability():
    m = _manager()
    champ = m._champions.all()[0].name_zh
    res = m.query_cooldown(champ, "R")
    assert res["kind"] == "ability" and res["base_cooldown"] > 0


# ---------------- Context（缺失即基础 CD） ----------------

def test_context_default_base():
    ctx = CooldownContext.empty()
    assert ctx.ability_haste == 0.0 and ctx.available is False
    # 无 haste → 基础 CD
    assert CooldownCalculator.for_ability(100.0, "Q",
        ability_haste=ctx.ability_haste, ultimate_haste=ctx.ultimate_haste) == 100.0


# ---------------- Intent ----------------

def _intent(text):
    from app.voice.intent import RuleBasedIntent
    return RuleBasedIntent.match(text)


def test_intent_start_samples():
    for txt in ["莎弥拉 W 用了", "莎弥拉 W 了", "莎弥拉 W 交了",
                "莎弥拉大招用了", "石头人 R 了",
                "对面莎弥拉闪现了"]:
        it = _intent(txt)
        assert it is not None, f"未匹配: {txt}"
        assert it.intent_type in ("cooldown.start", "alarm.spell", "alarm.flash"), \
            f"{txt} -> {it.intent_type}"


def test_intent_query_samples():
    for txt in ["石头人 R 冷却多久", "莎弥拉 W 多久", "闪现多久", "闪现 CD"]:
        it = _intent(txt)
        assert it is not None, f"未匹配: {txt}"
        assert it.intent_type == "cooldown.query", f"{txt} -> {it.intent_type}"


# ---------------- 直接运行入口 ----------------
if __name__ == "__main__":
    fns = [(k, v) for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = failed = 0
    for name, fn in fns:
        try:
            fn()
            passed += 1
            print(f"PASS {name}")
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL {name}: {e}")
            traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
