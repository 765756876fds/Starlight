"""ObjectiveTracker — 纯确定性中立资源状态机。

不依赖 EventBus / GameAlarm / 网络，可单测。
输入：规则集 + 游戏事件；输出：每个资源的状态与下次刷新绝对游戏时间。
实时调度仍由 ObjectiveTimer（→ GameAlarmEngine → SpeechJudge）完成。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional

from .rules import ObjectiveRule, ObjectiveRuleset


class ObjectiveState(str, Enum):
    NOT_ACTIVE = "not_active"
    WAITING_FIRST_SPAWN = "waiting_first_spawn"
    AVAILABLE = "available"
    KILLED = "killed"
    WAITING_RESPAWN = "waiting_respawn"
    READY_SOON = "ready_soon"
    GAME_ENDED = "game_ended"


@dataclass
class _Objective:
    rule: ObjectiveRule
    state: ObjectiveState = ObjectiveState.NOT_ACTIVE
    next_spawn: Optional[float] = None   # 下次刷新的绝对游戏时间（秒）
    killed: int = 0


class ObjectiveTracker:
    """按规则与事件推算各资源状态与下次刷新时间。"""

    def __init__(self, ruleset: ObjectiveRuleset):
        self.ruleset = ruleset
        self._lock = threading.Lock()
        self._objs: Dict[str, _Objective] = {}
        self._dragon_kills = 0
        self._elder_reached = False
        self.reset()

    def reset(self) -> None:
        with self._lock:
            self._objs = {
                r.type: _Objective(rule=r) for r in self.ruleset.enabled_types()
            }
            self._dragon_kills = 0
            self._elder_reached = False

    # ----- 事件入口 -----
    def on_game_start(self) -> None:
        self.reset()
        with self._lock:
            for o in self._objs.values():
                if o.rule.first_spawn is not None:
                    o.state = ObjectiveState.WAITING_FIRST_SPAWN
                    o.next_spawn = float(o.rule.first_spawn)
                else:
                    # elder 由 dragon soul 触发
                    o.state = ObjectiveState.NOT_ACTIVE
                    o.next_spawn = None

    def on_game_end(self) -> None:
        with self._lock:
            for o in self._objs.values():
                o.state = ObjectiveState.GAME_ENDED
                o.next_spawn = None

    def on_dragon_killed(self, game_time: float, dragon_type: str = "") -> None:
        with self._lock:
            if dragon_type == "Elder":
                e = self._objs.get("elder")
                if e and e.rule.respawn:
                    e.killed += 1
                    e.next_spawn = game_time + float(e.rule.respawn)
                    e.state = ObjectiveState.WAITING_RESPAWN
                return
            self._dragon_kills += 1
            d = self._objs.get("dragon")
            if d:
                d.killed += 1
                if d.rule.respawn:
                    d.next_spawn = game_time + float(d.rule.respawn)
                    d.state = ObjectiveState.WAITING_RESPAWN
            # 第 4 条龙 → dragon soul → 首次远古龙
            if not self._elder_reached and self._dragon_kills >= 4:
                self._elder_reached = True
                e = self._objs.get("elder")
                if e and e.rule.respawn:
                    e.next_spawn = game_time + float(e.rule.respawn)
                    e.state = ObjectiveState.WAITING_RESPAWN

    def on_baron_killed(self, game_time: float) -> None:
        with self._lock:
            b = self._objs.get("baron")
            if b and b.rule.respawn:
                b.killed += 1
                b.next_spawn = game_time + float(b.rule.respawn)
                b.state = ObjectiveState.WAITING_RESPAWN

    def on_herald_killed(self, game_time: float) -> None:
        with self._lock:
            g = self._objs.get("void_grubs")
            if g:
                g.killed += 1
                g.next_spawn = None
                g.state = ObjectiveState.KILLED
            h = self._objs.get("herald")
            if h:
                h.next_spawn = None
                h.state = ObjectiveState.KILLED

    # ----- 查询 -----
    def next_spawn_times(self) -> Dict[str, float]:
        """{objective_type: 下次刷新绝对游戏时间}，不含 None。"""
        with self._lock:
            return {t: o.next_spawn for t, o in self._objs.items()
                    if o.next_spawn is not None}

    def state_of(self, objective_type: str) -> ObjectiveState:
        with self._lock:
            o = self._objs.get(objective_type)
            return o.state if o else ObjectiveState.NOT_ACTIVE

    def snapshot(self) -> List[dict]:
        with self._lock:
            return [
                {"type": o.rule.type, "name": o.rule.name_zh,
                 "state": o.state.value, "next_spawn": o.next_spawn}
                for o in self._objs.values()
            ]
