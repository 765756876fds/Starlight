"""
GameAlarm - 通用游戏提醒引擎

不是单独的"龙计时器"，而是通用的游戏提醒系统。
支持：
- 中立资源刷新提醒（龙、男爵、潮虫）
- 闪现/技能冷却提醒（用户主动记录）
- 眼位计时
- 自定义计时器

核心原则：Read > Infer
优先使用游戏可读取的现成计时，固定规则只作为 fallback。
"""
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from ..core.state_store import StateStore, StateNamespace, get_state_store


class AlarmType(Enum):
    """提醒类型"""
    OBJECTIVE = "objective"       # 中立资源刷新
    FLASH = "flash"               # 闪现冷却
    SPELL = "spell"               # 技能冷却
    WARD = "ward"                 # 眼位
    CUSTOM = "custom"             # 自定义计时器


class AlarmStatus(Enum):
    """提醒状态"""
    ACTIVE = "active"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


@dataclass
class Alarm:
    """单个提醒"""
    alarm_id: str
    alarm_type: AlarmType
    title: str                    # 显示标题，如 "莎弥拉 闪现"
    game_time_start: float        # 开始时的游戏时间
    duration: float               # 持续时间（秒）
    lead_time: float = 10.0       # 提前多久提醒（秒）
    status: AlarmStatus = AlarmStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    triggered_at: Optional[float] = None
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def game_time_end(self) -> float:
        """结束时的游戏时间"""
        return self.game_time_start + self.duration

    def remaining(self, current_game_time: float) -> float:
        """剩余时间（秒）"""
        return max(0, self.game_time_end - current_game_time)

    def should_trigger(self, current_game_time: float) -> bool:
        """是否应该触发提醒"""
        if self.status != AlarmStatus.ACTIVE:
            return False
        remaining = self.remaining(current_game_time)
        return remaining <= self.lead_time

    def is_expired(self, current_game_time: float) -> bool:
        """是否已过期"""
        return self.remaining(current_game_time) <= 0


class GameAlarmEngine:
    """
    游戏提醒引擎

    通过 EventBus 接收事件，通过 Scheduler 定期检查。
    触发时发布 ALARM_TRIGGERED 事件，由 SpeechJudge 决定是否播报。
    """

    # 默认提前提醒时间（秒）
    DEFAULT_LEAD_TIMES = {
        AlarmType.OBJECTIVE: 40.0,
        AlarmType.FLASH: 15.0,
        AlarmType.SPELL: 10.0,
        AlarmType.WARD: 5.0,
        AlarmType.CUSTOM: 10.0,
    }

    # 默认持续时间（秒）
    DEFAULT_DURATIONS = {
        "flash": 300.0,       # 闪现 5 分钟
        "ward": 120.0,        # 普通眼 2 分钟
        "control_ward": 0.0,  # 真眼不消失
    }

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        self.config = config or {}
        self.alarms: Dict[str, Alarm] = {}
        self._lock = threading.Lock()
        self._current_game_time: float = 0.0
        self._running = False

        # 核心服务
        self.event_bus = get_event_bus()
        self.state_store = get_state_store()

        # 中立资源状态（从游戏数据读取）
        self._objective_states: Dict[str, Dict[str, Any]] = {}

        # 注册事件监听
        self._register_events()

    def start(self):
        """启动提醒引擎"""
        self._running = True
        print("[GameAlarm] 引擎已启动")

    def stop(self):
        """停止提醒引擎"""
        self._running = False
        with self._lock:
            self.alarms.clear()
        print("[GameAlarm] 引擎已停止")

    def _register_events(self):
        """注册事件监听"""
        # 游戏时间更新
        self.event_bus.subscribe(
            EventType.GAME_TIME_UPDATE,
            self._on_game_time_update
        )
        # 中立资源被杀
        self.event_bus.subscribe(
            EventType.DRAGON_KILLED,
            lambda e: self._on_objective_killed("dragon", e)
        )
        self.event_bus.subscribe(
            EventType.BARON_KILLED,
            lambda e: self._on_objective_killed("baron", e)
        )
        self.event_bus.subscribe(
            EventType.HERALD_KILLED,
            lambda e: self._on_objective_killed("herald", e)
        )
        # 游戏开始/结束
        self.event_bus.subscribe(EventType.GAME_STARTED, self._on_game_start)
        self.event_bus.subscribe(EventType.GAME_ENDED, self._on_game_end)

    # ===== 公共 API =====

    def create_flash_alarm(self, champion_name: str,
                            game_time: Optional[float] = None,
                            cooldown: float = 300.0) -> Alarm:
        """
        记录敌方闪现

        用户说："对面莎弥拉闪现了，帮我记一下"
        """
        gt = game_time if game_time is not None else self._current_game_time
        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.FLASH,
            title=f"{champion_name} 闪现",
            game_time_start=gt,
            duration=cooldown,
            lead_time=self.config.get("flash_lead", 15.0),
            data={"champion": champion_name, "spell": "flash"},
        )
        with self._lock:
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def create_spell_alarm(self, champion_name: str, spell_name: str,
                            cooldown: float,
                            game_time: Optional[float] = None) -> Alarm:
        """
        记录技能使用

        用户说："莎弥拉 W 用了"
        """
        gt = game_time if game_time is not None else self._current_game_time
        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.SPELL,
            title=f"{champion_name} {spell_name}",
            game_time_start=gt,
            duration=cooldown,
            lead_time=10.0,
            data={"champion": champion_name, "spell": spell_name},
        )
        with self._lock:
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def create_ward_alarm(self, location: str = "未知位置",
                           game_time: Optional[float] = None,
                           duration: float = 120.0) -> Alarm:
        """
        记录眼位

        用户说："这里插了个眼，帮我记一下"
        """
        gt = game_time if game_time is not None else self._current_game_time
        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.WARD,
            title=f"眼位 - {location}",
            game_time_start=gt,
            duration=duration,
            lead_time=5.0,
            data={"location": location},
        )
        with self._lock:
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def create_custom_alarm(self, title: str, duration: float,
                            game_time: Optional[float] = None,
                            lead_time: float = 10.0) -> Alarm:
        """
        创建自定义计时器

        用户说："两分钟后提醒我"
        """
        gt = game_time if game_time is not None else self._current_game_time
        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.CUSTOM,
            title=title,
            game_time_start=gt,
            duration=duration,
            lead_time=lead_time,
            data={},
        )
        with self._lock:
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def cancel_alarm(self, alarm_id: str):
        """取消提醒"""
        with self._lock:
            alarm = self.alarms.get(alarm_id)
            if alarm:
                alarm.status = AlarmStatus.CANCELLED

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CANCELLED,
            data={"alarm_id": alarm_id},
            source="game_alarm"
        ))

    def get_active_alarms(self) -> List[Alarm]:
        """获取所有活跃提醒"""
        with self._lock:
            return [a for a in self.alarms.values()
                    if a.status == AlarmStatus.ACTIVE]

    def get_alarm_by_id(self, alarm_id: str) -> Optional[Alarm]:
        """按 ID 获取提醒"""
        with self._lock:
            return self.alarms.get(alarm_id)

    def update_objective_state(self, objective_type: str, state: Dict[str, Any]):
        """
        更新中立资源状态（从游戏数据读取）

        Read > Infer：优先使用游戏提供的刷新时间
        """
        self._objective_states[objective_type] = state

    # ===== 事件处理 =====

    def _on_game_time_update(self, event: Event):
        """游戏时间更新 -> 检查提醒"""
        game_time = event.get("game_time", 0)
        self._current_game_time = game_time
        self._check_alarms(game_time)

    def _on_objective_killed(self, objective_type: str, event: Event):
        """
        中立资源被杀 -> 创建刷新提醒

        注意：这是 fallback。如果游戏能直接提供刷新时间，优先用游戏数据。
        """
        # 检查是否已有从游戏数据读取的刷新时间
        objective_state = self._objective_states.get(objective_type)
        if objective_state and objective_state.get("respawn_at"):
            # 游戏提供了刷新时间，用游戏数据
            respawn_at = objective_state["respawn_at"]
            duration = respawn_at - self._current_game_time
            lead_time = self._get_objective_lead(objective_type)
            self._create_objective_alarm(objective_type, duration, lead_time)
        else:
            # fallback：用固定规则
            durations = {
                "dragon": 300.0,   # 小龙 5 分钟
                "baron": 360.0,    # 男爵 6 分钟
                "herald": 360.0,   # 潮虫 6 分钟
            }
            duration = durations.get(objective_type, 300.0)
            lead_time = self._get_objective_lead(objective_type)
            self._create_objective_alarm(objective_type, duration, lead_time)

    def _create_objective_alarm(self, objective_type: str,
                                 duration: float, lead_time: float):
        """创建中立资源刷新提醒"""
        names = {
            "dragon": "小龙",
            "baron": "男爵",
            "herald": "潮虫先锋",
        }
        name = names.get(objective_type, objective_type)

        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.OBJECTIVE,
            title=f"{name}刷新",
            game_time_start=self._current_game_time,
            duration=duration,
            lead_time=lead_time,
            data={"objective_type": objective_type},
        )
        with self._lock:
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))

    def _get_objective_lead(self, objective_type: str) -> float:
        """获取中立资源提前提醒时间"""
        leads = {
            "dragon": self.config.get("dragon_lead", 40.0),
            "baron": self.config.get("baron_lead", 40.0),
            "herald": self.config.get("rift_herald_lead", 60.0),
        }
        return leads.get(objective_type, 40.0)

    def _on_game_start(self, event: Event):
        """游戏开始 -> 重置"""
        with self._lock:
            self.alarms.clear()
        self._current_game_time = 0.0
        self._objective_states.clear()

    def _on_game_end(self, event: Event):
        """游戏结束 -> 清理"""
        with self._lock:
            for alarm in self.alarms.values():
                alarm.status = AlarmStatus.EXPIRED

    # ===== 内部方法 =====

    def _check_alarms(self, game_time: float):
        """检查所有提醒，触发到期的"""
        to_trigger = []
        to_expire = []

        with self._lock:
            for alarm in self.alarms.values():
                if alarm.status != AlarmStatus.ACTIVE:
                    continue

                if alarm.should_trigger(game_time):
                    to_trigger.append(alarm)
                elif alarm.is_expired(game_time):
                    to_expire.append(alarm)

        # 触发提醒
        for alarm in to_trigger:
            alarm.status = AlarmStatus.TRIGGERED
            alarm.triggered_at = time.time()

            remaining = alarm.remaining(game_time)
            self.event_bus.publish(Event(
                event_type=EventType.ALARM_TRIGGERED,
                data={
                    "alarm": self._alarm_to_dict(alarm),
                    "remaining": remaining,
                    "game_time": game_time,
                },
                source="game_alarm"
            ))

        # 标记过期
        for alarm in to_expire:
            alarm.status = AlarmStatus.EXPIRED

        # 更新状态存储
        self._update_state()

    def _update_state(self):
        """更新状态存储"""
        active = self.get_active_alarms()
        self.state_store.set(StateNamespace.ALARM, "active_count", len(active))
        self.state_store.set(
            StateNamespace.ALARM,
            "alarms",
            [self._alarm_to_dict(a) for a in active]
        )

    def _alarm_to_dict(self, alarm: Alarm) -> Dict[str, Any]:
        """提醒转字典"""
        return {
            "alarm_id": alarm.alarm_id,
            "type": alarm.alarm_type.value,
            "title": alarm.title,
            "game_time_start": alarm.game_time_start,
            "duration": alarm.duration,
            "game_time_end": alarm.game_time_end,
            "lead_time": alarm.lead_time,
            "status": alarm.status.value,
            "remaining": alarm.remaining(self._current_game_time),
            "data": alarm.data,
        }
