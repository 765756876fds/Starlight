"""
ObjectiveTimer - 中立资源刷新提醒（实时接线层）。

状态与下次刷新时间由 ObjectiveTracker（纯确定性状态机）推算；
本类只负责订阅 EventBus 事件，并把提醒交给 GameAlarmEngine，
最终走 EventBus(ALARM_TRIGGERED) → SpeechJudge → SpeechQueue。
时间表来自版本化数据文件 data/lol_data/objectives.json（见 objectives.rules）。
"""
from __future__ import annotations

from ..core.event_bus import Event, EventType, get_event_bus
from .game_alarm import GameAlarmEngine
from .objectives import ObjectiveTracker, load_ruleset


class ObjectiveTimer:
    """中立资源刷新提醒（接线层）。"""

    def __init__(self, game_alarm: GameAlarmEngine, ruleset=None):
        self.game_alarm = game_alarm
        self.event_bus = get_event_bus()
        self.ruleset = ruleset or load_ruleset()
        self.tracker = ObjectiveTracker(self.ruleset)
        self._running = False
        # 去重：同一 (objective, spawn_time, lead) 只排一次提醒
        self._scheduled = set()

    def start(self):
        self._running = True
        self._first_spawns_scheduled = False
        self.event_bus.subscribe(EventType.GAME_STARTED, self._on_game_start)
        self.event_bus.subscribe(EventType.GAME_TIME_UPDATE, self._on_time_update)
        self.event_bus.subscribe(EventType.GAME_ENDED, self._on_game_end)
        self.event_bus.subscribe(EventType.DRAGON_KILLED, self._on_dragon_killed)
        self.event_bus.subscribe(EventType.BARON_KILLED, self._on_baron_killed)
        self.event_bus.subscribe(EventType.HERALD_KILLED, self._on_herald_killed)
        print(f"[ObjectiveTimer] 已启动 (patch={self.ruleset.patch}, "
              f"version={self.ruleset.data_version})")

    def stop(self):
        self._running = False
        self.tracker.on_game_end(); self._first_spawns_scheduled=True

    # ----- 事件处理 -----
    def _schedule_first_spawns(self) -> None:
        for o in self.tracker.snapshot():
            rule = self.ruleset.get(o["type"])
            if not rule or o["next_spawn"] is None:
                continue
            self._schedule_for(o["type"], rule.name_zh, o["next_spawn"], rule.leads)

    def _on_time_update(self, event: Event):
        # Agent 重启附着到进行中对局：首帧 GAME_STARTED 的 game_time 可能还是 0（连接未就绪），
        # 此时不能排首次刷新。等第一个真实 game_time(>0) 到达后再排一次。
        if self._first_spawns_scheduled:
            return
        gt = event.get("game_time", 0.0) or 0.0
        if gt <= 0:
            return
        try:
            self.game_alarm._current_game_time = float(gt)
        except Exception:
            pass
        self._first_spawns_scheduled = True
        print(f"[ObjectiveTimer] 首次排程按真实 game_time={gt:.0f}")
        self._schedule_first_spawns()

    def _on_game_start(self, event: Event):
        # 重启附着到进行中对局时，首帧可能 game_time=0。先同步已知时间；
        # 只有 game_time>0 才立即排程，否则等第一个 GAME_TIME_UPDATE。
        if not event:  # 防御：调用方误传 None（生产 EventBus 恒传 Event）
            return
        gt = float(event.get("game_time", 0.0) or 0.0)
        try:
            self.game_alarm._current_game_time = gt
        except Exception:
            pass
        self.tracker.on_game_start()
        self._scheduled.clear()
        self._first_spawns_scheduled = False
        print(f"[ObjectiveTimer] GAME_STARTED game_time={gt:.0f}, "
              f"alarm_now={getattr(self.game_alarm, '_current_game_time', 0):.0f}")
        if gt > 0:
            self._first_spawns_scheduled = True
            self._schedule_first_spawns()

    def _on_game_end(self, event: Event):
        self.tracker.on_game_end(); self._first_spawns_scheduled=True
        self._scheduled.clear()

    def _on_dragon_killed(self, event: Event):
        game_time = event.get("game_time", 0.0) or 0.0
        dragon_type = event.data.get("dragon_type", "")
        killer_team = event.data.get("killer_team", "")
        self.tracker.on_dragon_killed(game_time, dragon_type, killer_team)

        if dragon_type == "Elder":
            t = self.tracker.next_spawn_times().get("elder")
            if t is not None:
                rule = self.ruleset.get("elder")
                self._schedule_for("elder", rule.name_zh, t, rule.leads)
            return

        t = self.tracker.next_spawn_times().get("dragon")
        if t is not None:
            rule = self.ruleset.get("dragon")
            self._schedule_for("dragon", rule.name_zh, t, rule.leads)
        # 第 4 条龙触发的首次远古龙（尚未被击杀过）
        e = self.tracker._objs.get("elder")
        if e is not None and e.killed == 0 and e.next_spawn is not None:
            rule = self.ruleset.get("elder")
            self._schedule_for("elder", rule.name_zh, e.next_spawn, rule.leads)

    def _on_baron_killed(self, event: Event):
        game_time = event.get("game_time", 0.0) or 0.0
        self.tracker.on_baron_killed(game_time)
        t = self.tracker.next_spawn_times().get("baron")
        if t is not None:
            rule = self.ruleset.get("baron")
            self._schedule_for("baron", rule.name_zh, t, rule.leads)

    def _on_herald_killed(self, event: Event):
        self.tracker.on_herald_killed(event.get("game_time", 0.0) or 0.0)

    # ----- 内部 -----
    def _schedule_for(self, obj_type: str, name_zh: str,
                      spawn_at: float, leads) -> None:
        """为一个资源的多个 lead 点排提醒（同 objective/spawn/lead 去重）。"""
        for lead in leads:
            key = (obj_type, round(float(spawn_at), 2), round(float(lead), 2))
            if key in self._scheduled:
                continue
            now = getattr(self.game_alarm, "_current_game_time", 0.0) or 0.0
            if float(spawn_at) - float(lead) <= now:
                continue
            self._scheduled.add(key)
            self.game_alarm.schedule_objective_reminder(
                f"{int(lead)}秒后{name_zh}刷新",
                spawn_at,
                float(lead),
                {"objective": obj_type}
            )

    def get_next_timers(self) -> list[dict]:
        """当前活跃资源的下次刷新倒计时（游戏内秒）。供 UI/查询使用。"""
        now = getattr(self.game_alarm, "_current_game_time", 0.0) or 0.0
        out = []
        for t, spawn_at in self.tracker.next_spawn_times().items():
            remaining = spawn_at - now
            if remaining > 0:
                rule = self.ruleset.get(t)
                out.append({
                    "name": rule.name_zh if rule else t,
                    "seconds_remaining": round(remaining, 1),
                })
        out.sort(key=lambda x: x["seconds_remaining"])
        return out
