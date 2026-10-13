"""PHASE B-FIX 端到端测试。"""
import time

import pytest

from app.core.command_bus import Command, CommandType
from app.lol.cooldown import CooldownManager
from app.lol.cooldown.events import COOLDOWN_REMIND, COOLDOWN_READY
from app.lol.data import AbilityCatalog, ChampionCatalog, SummonerSpellCatalog
from app.voice.intent import RuleBasedIntent


class FakeScheduler:
    """不真等时间的 Scheduler：schedule_once 立即记录，trigger() 手动触发。"""
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

    def trigger_all(self):
        for cb in list(self.tasks.values()):
            cb()
        self.tasks.clear()


def make_mgr(events, speeches, sched=None):
    sched = sched or FakeScheduler()
    mgr = CooldownManager(
        champions=ChampionCatalog(),
        abilities=AbilityCatalog(),
        spells=SummonerSpellCatalog(),
        on_event=lambda c, d: events.append((c, d)),
        speech_enqueue=lambda text, eid: speeches.append((text, eid)),
        remind_before=5.0,
        scheduler=sched,
    )
    mgr.set_game_time(0.0)  # 同步游戏时钟：未同步时 start_reminder 返回 None
    return mgr, sched


def test_real_catalog_loaded():
    ch = ChampionCatalog()
    abilities = AbilityCatalog()
    assert len(ch.all()) >= 150, f"champions={len(ch.all())}"
    yasuo = ch.get_by_id(157)
    assert yasuo is not None
    assert yasuo.name_zh == "亚索"
    assert abilities.get_cooldown(157, "R") is not None


def test_full_chain_enemy_flash():
    events, speeches = [], []
    mgr, sched = make_mgr(events, speeches)
    it = RuleBasedIntent.match("敌方亚索闪现交了")
    assert it.intent_type == "cooldown.start"
    cmd = Command(command_type=CommandType.COOLDOWN_START,
                   params=it.params)
    reminder = mgr.handle_start(cmd)
    assert reminder is not None
    assert reminder.champion_id == 157
    assert reminder.skill_type == "summoner_spell"
    assert reminder.base_cooldown == 300.0
    assert reminder.side == "enemy"

    # 触发 remind：推进游戏时钟到提醒点（新架构：无 cd-remind 任务，set_game_time 驱动）
    mgr.set_game_time(reminder.remind_at)
    assert any(c == COOLDOWN_REMIND for c, _ in events)
    assert speeches, "SpeechQueue should receive text"
    text, eid = speeches[-1]
    assert "敌方亚索闪现" in text
    assert "应该快好了" in text  # _remind_text：召唤师技能 remind_before>=20 → "应该快好了"

    # ready
    mgr.set_game_time(reminder.ready_at)
    assert any(c == COOLDOWN_READY for c, _ in events)


def test_full_chain_cancel():
    events, speeches = [], []
    mgr, sched = make_mgr(events, speeches)
    r = mgr.start_reminder("亚索", "闪现")
    cmd = Command(command_type=CommandType.COOLDOWN_CANCEL,
                   params={"champion": "亚索"})
    ok = mgr.handle_cancel(cmd)
    assert ok is True
    assert mgr.get(r.id) is None
    # 任务已取消
    assert f"cd-remind-{r.id}" in sched.cancelled or f"cd-remind-{r.id}" not in sched.tasks
