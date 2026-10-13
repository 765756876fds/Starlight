"""PHASE B 测试：data catalog + cooldown manager（用 FakeScheduler）。"""
import threading
import time

import pytest

from app.lol.data import (
    AbilityCatalog, ChampionCatalog, SummonerSpellCatalog,
)
from app.lol.cooldown import (
    CooldownManager, CooldownReminder, ReminderStatus,
    COOLDOWN_REMIND, COOLDOWN_READY, COOLDOWN_STARTED, COOLDOWN_CANCELLED,
)
from app.voice.intent import RuleBasedIntent


class FakeScheduler:
    def __init__(self):
        self.tasks = {}
        self.cancelled = set()

    def schedule_once(self, delay, callback, task_id=None):
        self.tasks[task_id] = callback
        return task_id

    def cancel_once(self, task_id):
        self.cancelled.add(task_id)
        self.tasks.pop(task_id, None)

    def trigger(self, task_id):
        cb = self.tasks.pop(task_id, None)
        if cb:
            cb()


def make_mgr(events=None, remind_before=5.0):
    events = events if events is not None else []
    sched = FakeScheduler()
    mgr = CooldownManager(
        champions=ChampionCatalog(),
        abilities=AbilityCatalog(),
        spells=SummonerSpellCatalog(),
        on_event=lambda c, d: events.append((c, d)),
        speech_enqueue=None,
        remind_before=remind_before,
        scheduler=sched,
    )
    mgr.set_game_time(0.0)  # 同步游戏时钟：未同步时 start_reminder 返回 None
    return mgr, sched, events


# ---------- data layer ----------
class TestSummonerSpellCatalog:
    def test_flash_zh(self):
        c = SummonerSpellCatalog()
        sp = c.get_by_name("闪现")
        assert sp is not None and sp.name_en == "Flash" and sp.cooldown == 300.0

    def test_teleport(self):
        c = SummonerSpellCatalog()
        # 数据更新后：传送 CD 300s
        assert c.get_by_name("Teleport").cooldown == 300.0
        assert c.get_by_name("传送").cooldown == 300.0

    def test_get_by_id(self):
        c = SummonerSpellCatalog()
        assert c.get_by_id(4).name_en == "Flash"
        assert c.get_by_id(999) is None


class TestChampionCatalog:
    def test_bulk_loaded(self):
        c = ChampionCatalog()
        assert len(c.all()) >= 150

    def test_yasuo(self):
        c = ChampionCatalog()
        assert c.get_by_id(157).name_zh == "亚索"
        assert c.get_by_name("亚索").cid == 157


class TestAbilityCatalog:
    def test_yasuo_r(self):
        c = AbilityCatalog()
        assert c.get_cooldown(157, "R") is not None


# ---------- cooldown manager ----------
class TestCooldownManager:
    def test_start_summoner_spell(self):
        mgr, sched, ev = make_mgr()
        r = mgr.start_reminder("亚索", "闪现", side="enemy")
        assert r is not None
        assert r.champion_id == 157
        assert r.skill_type == "summoner_spell"
        assert r.base_cooldown == 300.0
        assert ev[0][0] == COOLDOWN_STARTED
        mgr.cancel_all()

    def test_start_ability(self):
        mgr, sched, ev = make_mgr()
        r = mgr.start_reminder("亚索", "R")
        assert r.skill == "大招"
        mgr.cancel_all()

    def test_remind_fires(self):
        ev = []
        mgr, sched, ev = make_mgr(events=ev, remind_before=5.0)
        from app.lol.data.summoner_spell_catalog import SummonerSpell
        mgr._spells._by_zh["测试技能"] = SummonerSpell(999, "T", "测试技能", 0.2)
        r = mgr.start_reminder("亚索", "测试技能")
        sched.trigger(f"cd-remind-{r.id}")
        types = [c for c, _ in ev]
        assert COOLDOWN_REMIND in types
        remind = next(d for c, d in ev if c == COOLDOWN_REMIND)
        assert "亚索" in remind["text"]
        sched.trigger(f"cd-ready-{r.id}")

    def test_ready_removes(self):
        ev = []
        mgr, sched, ev = make_mgr(events=ev)
        r = mgr.start_reminder("亚索", "闪现")
        mgr.set_game_time(r.ready_at)  # 新架构：推进时钟到 ready 点
        assert COOLDOWN_READY in [c for c, _ in ev]
        assert mgr.get(r.id) is None

    def test_cancel_no_fire(self):
        ev = []
        mgr, sched, ev = make_mgr(events=ev)
        r = mgr.start_reminder("亚索", "闪现")
        assert mgr.cancel(r.id) is True
        assert f"cd-remind-{r.id}" not in sched.tasks
        assert COOLDOWN_CANCELLED in [c for c, _ in ev]

    def test_cancel_all(self):
        mgr, sched, ev = make_mgr()
        mgr.start_reminder("亚索", "闪现")
        mgr.start_reminder("艾希", "R")
        assert mgr.cancel_all() == 2
        assert mgr.list_active() == []

    def test_unknown_skill(self):
        mgr, sched, ev = make_mgr()
        assert mgr.start_reminder("亚索", "不存在") is None
        assert mgr.start_reminder(None, "W") is None


# ---------- intent rules ----------
class TestCooldownIntent:
    def test_enemy_flash(self):
        it = RuleBasedIntent.match("敌方亚索闪现交了")
        assert it.intent_type == "cooldown.start"
        assert it.params["champion"] == "亚索"
        assert it.params["skill"] == "闪现"
        assert it.params["side"] == "enemy"

    def test_no_prefix_default_enemy(self):
        it = RuleBasedIntent.match("亚索闪现用了")
        assert it.params["side"] == "enemy"

    def test_ally_w(self):
        it = RuleBasedIntent.match("我方亚索W交了")
        assert it.params["champion"] == "亚索"
        assert it.params["side"] == "ally"

    def test_championless_w(self):
        it = RuleBasedIntent.match("对面W交了")
        assert it.intent_type == "cooldown.start"
        assert it.params["skill"] == "W"

    def test_cancel_all(self):
        it = RuleBasedIntent.match("取消所有计时")
        assert it.intent_type == "cooldown.cancel_all"
