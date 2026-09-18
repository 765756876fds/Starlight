"""
ObjectiveTimer - 中立资源刷新计时器

根据冻结规则计算各资源的首次刷新和后续刷新时间，
通过 GameAlarmEngine 安排提醒。

规则：
- Void Grubs (巢虫): 首次 8:00(480s), 7:00(420s)提醒, lead=60s, 不刷新
- Rift Herald (先锋): 首次 15:00(900s), 14:00(840s)提醒, lead=60s, 不刷新
- Dragon (小龙): 首次 5:00(300s), 4:20(260s)提醒, lead=40s, 击杀后+300s
- Baron (大龙): 首次 20:00(1200s), 19:20(1160s)提醒, lead=40s, 击杀后+360s
- Elder (远古龙): Soul后+360s, 击杀后+360s, lead=40s
"""
from typing import Optional

from ..core.event_bus import Event, EventType, get_event_bus
from .game_alarm import GameAlarmEngine


class ObjectiveTimer:
    """中立资源刷新计时器"""

    # 固定时间（游戏内秒）
    VOID_GRUBS_SPAWN = 480.0
    VOID_GRUBS_LEAD = 60.0

    HERALD_SPAWN = 900.0
    HERALD_LEAD = 60.0

    DRAGON_FIRST_SPAWN = 300.0
    DRAGON_RESPAWN = 300.0
    DRAGON_LEAD = 40.0

    BARON_FIRST_SPAWN = 1200.0
    BARON_RESPAWN = 360.0
    BARON_LEAD = 40.0

    ELDER_RESPAWN = 360.0
    ELDER_LEAD = 40.0

    DRAGON_SOUL_KILLS = 4  # 4 条小龙后获得 Soul

    def __init__(self, game_alarm: GameAlarmEngine):
        self.game_alarm = game_alarm
        self.event_bus = get_event_bus()

        # 状态
        self._void_grubs_done = False
        self._herald_done = False
        self._dragon_kill_count = 0
        self._elder_soul_reached = False
        self._running = False
        # 下次刷新时间（游戏内秒）；None = 暂无
        self._game_started = False
        self._next_void_grubs: Optional[float] = None
        self._next_herald: Optional[float] = None
        self._next_dragon: Optional[float] = None
        self._next_baron: Optional[float] = None
        self._next_elder: Optional[float] = None

    def start(self):
        self._running = True
        self.event_bus.subscribe(EventType.GAME_STARTED, self._on_game_start)
        self.event_bus.subscribe(EventType.DRAGON_KILLED, self._on_dragon_killed)
        self.event_bus.subscribe(EventType.BARON_KILLED, self._on_baron_killed)
        self.event_bus.subscribe(EventType.HERALD_KILLED, self._on_herald_killed)
        print("[ObjectiveTimer] 已启动")

    def stop(self):
        self._running = False

    def _on_game_start(self, event: Event):
        """游戏开始 -> 安排所有首次刷新提醒"""
        self._void_grubs_done = False
        self._herald_done = False
        self._dragon_kill_count = 0
        self._elder_soul_reached = False
        self._game_started = True
        self._next_void_grubs = self.VOID_GRUBS_SPAWN
        self._next_herald = self.HERALD_SPAWN
        self._next_dragon = self.DRAGON_FIRST_SPAWN
        self._next_baron = self.BARON_FIRST_SPAWN
        self._next_elder = None

        now = self.game_alarm._current_game_time

        # 巢虫
        self.game_alarm.schedule_objective_reminder(
            "60秒后巢虫刷新",
            self.VOID_GRUBS_SPAWN,
            self.VOID_GRUBS_LEAD,
            {"objective": "void_grubs"}
        )

        # 先锋
        self.game_alarm.schedule_objective_reminder(
            "60秒后先锋刷新",
            self.HERALD_SPAWN,
            self.HERALD_LEAD,
            {"objective": "herald"}
        )

        # 小龙
        self.game_alarm.schedule_objective_reminder(
            "40秒后小龙刷新",
            self.DRAGON_FIRST_SPAWN,
            self.DRAGON_LEAD,
            {"objective": "dragon"}
        )

        # 大龙
        self.game_alarm.schedule_objective_reminder(
            "40秒后大龙刷新",
            self.BARON_FIRST_SPAWN,
            self.BARON_LEAD,
            {"objective": "baron"}
        )

    def _on_dragon_killed(self, event: Event):
        """小龙/远古龙被击杀"""
        game_time = event.get("game_time", 0)
        dragon_type = event.data.get("dragon_type", "")

        if dragon_type == "Elder":
            # 远古龙击杀 -> 安排下一次远古龙
            self._next_elder = game_time + self.ELDER_RESPAWN
            self.game_alarm.schedule_objective_reminder(
                "40秒后远古龙刷新",
                self._next_elder,
                self.ELDER_LEAD,
                {"objective": "elder"}
            )
            return

        # 普通小龙
        self._dragon_kill_count += 1

        # 安排下一条普通小龙
        self._next_dragon = game_time + self.DRAGON_RESPAWN
        self.game_alarm.schedule_objective_reminder(
            "40秒后小龙刷新",
            self._next_dragon,
            self.DRAGON_LEAD,
            {"objective": "dragon"}
        )

        # 检查是否达成 Dragon Soul
        if not self._elder_soul_reached and self._dragon_kill_count >= self.DRAGON_SOUL_KILLS:
            self._elder_soul_reached = True
            self._next_elder = game_time + self.ELDER_RESPAWN
            # Soul 达成后 6 分钟首次 Elder
            self.game_alarm.schedule_objective_reminder(
                "40秒后远古龙刷新",
                self._next_elder,
                self.ELDER_LEAD,
                {"objective": "elder"}
            )

    def _on_baron_killed(self, event: Event):
        """大龙被击杀 -> 安排下一次"""
        game_time = event.get("game_time", 0)
        self._next_baron = game_time + self.BARON_RESPAWN
        self.game_alarm.schedule_objective_reminder(
            "40秒后大龙刷新",
            self._next_baron,
            self.BARON_LEAD,
            {"objective": "baron"}
        )

    def _on_herald_killed(self, event: Event):
        """先锋被击杀 -> 一次性资源，不再安排"""
        self._herald_done = True
        self._next_herald = None
        self._next_void_grubs = None

    def get_next_timers(self) -> list:
        """返回当前活跃目标的下次刷新倒计时（游戏内秒）。"""
        if not self._game_started:
            return []
        now = getattr(self.game_alarm, "_current_game_time", 0) or 0
        out = []
        for label, t in (
            ("小龙", self._next_dragon),
            ("大龙", self._next_baron),
            ("先锋", self._next_herald),
            ("远古龙", self._next_elder),
        ):
            if t is None:
                continue
            remaining = t - now
            if remaining > 0:
                out.append({"name": label, "seconds_remaining": round(remaining, 1)})
        out.sort(key=lambda x: x["seconds_remaining"])
        return out
