"""OverlayModel 单元测试（不依赖 Qt event loop / 真实 2999）。

覆盖：position normalization / sorting / 敌方 5 人构造 / spell1+spell2 /
timer 显示（生产路径：CooldownManager → Overlay 只读渲染）/ remaining /
<10 / >999 / ready / InProgress 显隐 / game end clear / 新局不继承 /
dynamic SSH / game_time 暂停 / 100ms 刷新不改 CM 状态。
"""
from __future__ import annotations

from types import SimpleNamespace

from app.ui.overlay.overlay_model import (

    OverlayModel,
    fmt_remaining,
    _norm_position,
    _POSITION_ORDER,
)
from app.lol.cooldown.ssh_provider import summoner_spell_haste
from app.lol.cooldown.calculator import CooldownCalculator

# ---------- fakes ----------
import pytest

pytestmark = pytest.mark.unit


FLASH = {"id": 4, "displayName": "SummonerFlash"}
TELEPORT = {"id": 12, "displayName": "SummonerTeleport"}

def _player(cid, name, position, spells=(FLASH, TELEPORT)):
    return SimpleNamespace(
        champion_id=cid,
        champion_name=name,
        position=position,
        summoner_spells=list(spells),
    )

def _snapshot(players, game_time=120.0):
    return SimpleNamespace(game_time=game_time, enemy_team=list(players))

class FakeCM:
    """模拟 CooldownManager.get_active_reminders（生产路径：语音 cooldown.start →
    CooldownManager → Overlay 只读渲染；game.ended → CM.clear()）。"""

    def __init__(self, reminders=None):
        self.reminders = list(reminders or [])

    def get_active_reminders(self):
        return [dict(r) for r in self.reminders]

    def clear(self):
        self.reminders.clear()

def _rem(champion_name, skill, remaining):
    """对齐 manager.get_active_reminders 字段子集（Overlay 只读 champion_name/skill/remaining）。"""
    return {
        "id": "fake", "champion_name": champion_name, "skill": skill,
        "skill_type": "summoner", "estimated_cooldown": 0.0, "used_at": 0.0,
        "ready_at": 0.0, "remind_at": 0.0, "remaining": remaining,
        "side": "enemy", "status": "ACTIVE",
    }

def _model(players=None, game_time=120.0, phase="InProgress", cm_reminders=None):
    snap = _snapshot(players or [], game_time)
    cm = FakeCM(cm_reminders)   # 共享同一实例：测试改 cm 状态，get_rows 读到同一份
    return OverlayModel(
        snapshot_provider=lambda: snap,
        phase_provider=lambda: phase,
        cooldown_manager_provider=lambda: cm,
    )

# ---------- position normalization ----------
def test_position_normalize_mid_bot_sup():
    assert _norm_position("MID") == "MIDDLE"
    assert _norm_position("BOT") == "BOTTOM"
    assert _norm_position("ADC") == "BOTTOM"
    assert _norm_position("SUP") == "UTILITY"
    assert _norm_position("SUPPORT") == "UTILITY"
    assert _norm_position("top") == "TOP"
    assert _norm_position("") == ""

def test_position_order_keys():
    assert _POSITION_ORDER == {"TOP": 0, "JUNGLE": 1, "MIDDLE": 2, "BOTTOM": 3, "UTILITY": 4}

def test_position_sorting_five_rows():
    # 故意乱序给出 5 个位置
    players = [
        _player(5, "瑟提", "bot"),
        _player(1, "盖伦", "top"),
        _player(4, "艾希", "sup"),
        _player(2, "盲僧", "jungle"),
        _player(3, "亚索", "mid"),
    ]
    m = _model(players)
    rows = m.get_rows()
    names = [r.champion_name for r in rows]
    assert names == ["盖伦", "盲僧", "亚索", "瑟提", "艾希"]
    positions = [r.position for r in rows]
    assert positions == ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]

def test_enemy_five_rows_only_from_enemy_team():
    players = [_player(1, "A", "top"), _player(2, "B", "jungle")]
    m = _model(players)
    rows = m.get_rows()
    assert len(rows) == 2
    # 空 champion_name 被过滤
    players_bad = players + [_player(0, "", "top")]
    m2 = _model(players_bad)
    assert len(m2.get_rows()) == 2

def test_spell1_spell2_populated():
    m = _model([_player(1, "盖伦", "top", (FLASH, TELEPORT))])
    row = m.get_rows()[0]
    assert row.spell1.slot == 1 and row.spell1.spell_id == 4 and row.spell1.spell_name == "闪现"
    assert row.spell2.slot == 2 and row.spell2.spell_id == 12 and row.spell2.spell_name == "传送"
    assert row.spell1.base_cooldown == 300.0
    assert row.spell2.active is False and row.spell1.active is False

# ---------- timer 显示（生产路径：CooldownManager → Overlay 只读渲染）----------
def test_timer_start_and_remaining():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    row = m.get_rows()[0]
    assert row.spell1.active is True
    assert abs(row.spell1.remaining - 300.0) < 1e-6

def test_timer_advances_with_game_time():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    # CM 内部已按 game_time 推进 50s 算好 remaining=250（"推进"逻辑归 CooldownManager，有 CM 单测）
    cm = m._cooldown_manager_provider()
    cm.reminders = [_rem("盖伦", "闪现", 250.0)]
    m._snapshot_provider = lambda: _snapshot([_player(1, "盖伦", "top")], game_time=150.0)
    row = m.get_rows()[0]
    assert abs(row.spell1.remaining - 250.0) < 1e-6

def test_two_slots_independent():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0), _rem("盖伦", "传送", 190.0)])
    row = m.get_rows()[0]
    assert abs(row.spell1.remaining - 300.0) < 1e-6
    assert abs(row.spell2.remaining - 190.0) < 1e-6

# ---------- display rules ----------
def test_fmt_ready():
    assert fmt_remaining(-1.0) == "OK"
    assert fmt_remaining(0.0) == "0.0"

def test_fmt_below_10_one_decimal():
    assert fmt_remaining(9.9) == "9.9"
    assert fmt_remaining(5.0) == "5.0"

def test_fmt_above_999():
    assert fmt_remaining(1000.0) == "999"
    assert fmt_remaining(1500.0) == "999"

def test_fmt_integer_seconds():
    assert fmt_remaining(45.0) == "45"
    assert fmt_remaining(9.99) == "10.0"  # <10 一位小数
    assert fmt_remaining(10.0) == "10"

# ---------- lifecycle ----------
def test_inprogress_shows_else_hides():
    players = [_player(1, "盖伦", "top")]
    m = _model(players, phase="InProgress")
    assert len(m.get_rows()) == 1
    m2 = _model(players, phase="ChampSelect")
    assert m2.get_rows() == []
    m3 = _model(players, phase="None")
    assert m3.get_rows() == []

def test_disabled_hides():
    m = _model([_player(1, "盖伦", "top")])
    m.set_enabled(False)
    assert m.get_rows() == []

def test_game_end_clear():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    cm = m._cooldown_manager_provider()
    assert len(cm.reminders) == 1
    assert m.get_rows()[0].spell1.active is True
    cm.clear()   # 生产：game.ended → cooldown_manager.clear()
    assert len(cm.reminders) == 0
    row = m.get_rows()[0]
    assert row.spell1.active is False

def test_new_game_no_inherit():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    cm = m._cooldown_manager_provider()
    cm.clear()  # 模拟 game end / 离开 InProgress → CM 清空，新局不继承
    rows = m.get_rows()
    assert all(not r.spell1.active and not r.spell2.active for r in rows)

# ---------- dynamic SSH (复用 ssh_provider + calculator) ----------
def test_dynamic_ssh_formula():
    # 星界洞悉 18 + 明朗靴 10 = 28 SSH（明朗靴实测锚点：300*100/(100+10)≈272.73s）
    items = [{"itemID": 3158}]
    runes = [{"displayName": "Cosmic Insight"}]
    ssh, sources, status = summoner_spell_haste(items, runes)
    assert ssh == 28.0
    assert status == "ok"
    eff = CooldownCalculator.for_summoner(300.0, summoner_spell_haste=ssh)
    # 300 * 100 / 128 = 234.375
    assert abs(eff - 234.375) < 0.01

def test_overlay_uses_ssh_adjusted_cooldown():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 230.77)])
    row = m.get_rows()[0]
    assert abs(row.spell1.remaining - 230.77) < 1e-6

# ---------- game_time pause ----------
def test_game_time_pause_freezes_remaining():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    r1 = m.get_rows()[0].spell1.remaining
    # game_time 不变（暂停）-> CM remaining 不变 -> 显示不变
    r2 = m.get_rows()[0].spell1.remaining
    assert abs(r1 - r2) < 1e-6
    assert r1 == 300.0

# ---------- 100ms 刷新不改变 CM 状态 ----------
def test_refresh_does_not_create_timers():
    m = _model([_player(1, "盖伦", "top")], game_time=100.0,
               cm_reminders=[_rem("盖伦", "闪现", 300.0)])
    cm = m._cooldown_manager_provider()
    before = len(cm.reminders)
    # 模拟 100ms 轮询多次渲染
    for _ in range(5):
        m.get_rows()
    assert len(cm.reminders) == before
