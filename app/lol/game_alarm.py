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
from typing import Any

from ..core.event_bus import Event, EventType, get_event_bus
from ..core.state_store import StateNamespace, get_state_store


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
    game_time_start: float        # 开始时的基准时间（game=游戏时间 / wall=time.monotonic()）
    duration: float               # 持续时间（秒）
    lead_time: float = 10.0       # 提前多久提醒（秒）
    time_base: str = "game"       # 计时基准："game"=游戏时间（默认）/"wall"=现实时间
    status: AlarmStatus = AlarmStatus.ACTIVE
    created_at: float = field(default_factory=time.time)
    triggered_at: float | None = None
    data: dict[str, Any] = field(default_factory=dict)

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

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or {}
        self.alarms: dict[str, Alarm] = {}
        self._lock = threading.Lock()
        self._current_game_time: float = 0.0
        self._running = False
        self._wall_thread: threading.Thread | None = None

        # 核心服务
        self.event_bus = get_event_bus()
        self.state_store = get_state_store()

        # 中立资源状态（从游戏数据读取）
        self._objective_states: dict[str, dict[str, Any]] = {}

        # 注册事件监听
        self._register_events()

    def start(self):
        """启动提醒引擎"""
        self._running = True
        self._wall_thread = threading.Thread(
            target=self._wall_loop, daemon=True, name="game-alarm-wall")
        self._wall_thread.start()
        print("[GameAlarm] 引擎已启动")

    def stop(self):
        """停止提醒引擎"""
        self._running = False
        if self._wall_thread and self._wall_thread.is_alive():
            self._wall_thread.join(timeout=1.0)
        self._wall_thread = None
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
        # 游戏开始/结束
        self.event_bus.subscribe(EventType.GAME_STARTED, self._on_game_start)
        self.event_bus.subscribe(EventType.GAME_ENDED, self._on_game_end)

    # ===== 公共 API =====

    def create_flash_alarm(self, champion_name: str,
                            game_time: float | None = None,
                            cooldown: float = 300.0,
                            source_snapshot: dict[str, Any] | None = None) -> Alarm | None:
        """
        记录敌方闪现

        用户说："对面莎弥拉闪现了，帮我记一下"
        cooldown：调用方按 catalog base + 当前 SSH 计算后传入（T0 冻结）。
        同一 (champion, spell=flash) 已有 active 时返回已有 alarm，不重复建。
        """
        gt = game_time if game_time is not None else self._current_game_time
        spell = "flash"
        with self._lock:
            for a in self.alarms.values():
                if (a.status == AlarmStatus.ACTIVE
                        and a.data.get("champion") == champion_name
                        and a.data.get("spell") == spell):
                    return a
            alarm = Alarm(
                alarm_id=str(uuid.uuid4()),
                alarm_type=AlarmType.FLASH,
                title=f"{champion_name} 闪现",
                game_time_start=gt,
                duration=cooldown,
                lead_time=self.config.get("flash_lead", 15.0),
                data={
                    "champion": champion_name, "spell": spell, "source": "USER_MEMORY",
                    "base_cooldown": cooldown,
                    "source_snapshot": source_snapshot or {},
                },
            )
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def create_spell_alarm(self, champion_name: str, spell_name: str,
                            cooldown: float,
                            game_time: float | None = None) -> Alarm:
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
            data={"champion": champion_name, "spell": spell_name, "source": "USER_MEMORY"},
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
                           game_time: float | None = None,
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
            data={"location": location, "source": "USER_MEMORY"},
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
                            game_time: float | None = None,
                            lead_time: float = 10.0,
                            content: str = "") -> Alarm:
        """
        创建自定义计时器

        用户说："两分钟后提醒我"
        """
        # 显式传 game_time → 对局内计时（game 基准）；不传 → 现实时间（wall 基准，非对局也能触发）
        if game_time is not None:
            _base, _start = "game", game_time
        else:
            _base, _start = "wall", time.monotonic()
        alarm = Alarm(
            alarm_id=str(uuid.uuid4()),
            alarm_type=AlarmType.CUSTOM,
            title=title,
            game_time_start=_start,
            duration=duration,
            lead_time=lead_time,
            time_base=_base,
            data={"source": "USER_MEMORY", "content": content},
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

    def schedule_objective_reminder(self, title: str, game_time_end: float,
                                     lead_time: float = 40.0,
                                     data: dict | None = None) -> Alarm | None:
        """
        创建一个在指定游戏时间提前 lead_time 秒触发的提醒。

        game_time_end: 资源刷新的绝对游戏时间（秒）
        lead_time: 提前多少秒提醒
        如果当前游戏时间已经超过 game_time_end - lead_time，则不创建（不补播）。
        """
        with self._lock:
            # 不补播：如果提醒时间已过，跳过
            if self._current_game_time >= game_time_end - lead_time:
                return None

            alarm = Alarm(
                alarm_id=str(uuid.uuid4()),
                alarm_type=AlarmType.OBJECTIVE,
                title=title,
                game_time_start=self._current_game_time,
                duration=game_time_end - self._current_game_time,
                lead_time=lead_time,
                data={**(data or {}), "source": "SYSTEM"},
            )
            self.alarms[alarm.alarm_id] = alarm

        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CREATED,
            data={"alarm": self._alarm_to_dict(alarm)},
            source="game_alarm"
        ))
        return alarm

    def get_active_alarms(self) -> list[Alarm]:
        """获取所有活跃提醒"""
        with self._lock:
            return [a for a in self.alarms.values()
                    if a.status == AlarmStatus.ACTIVE]

    def get_user_memories(self) -> list[Alarm]:
        """获取用户主动创建的临时记忆（USER_MEMORY，排除系统提醒）"""
        with self._lock:
            return [a for a in self.alarms.values()
                    if a.status == AlarmStatus.ACTIVE
                    and (a.data or {}).get("source", "USER_MEMORY") == "USER_MEMORY"]

    def cancel_last_user_memory(self):
        """取消最近一个用户临时记忆，返回被取消的 Alarm 或 None"""
        with self._lock:
            # 按字典插入顺序（即创建顺序）取最近一条用户记忆。
            # 不使用 created_at：同一秒创建的多条记忆时间相同，排序会不稳定。
            a = None
            for v in self.alarms.values():
                if v.status == AlarmStatus.ACTIVE \
                        and (v.data or {}).get("source", "USER_MEMORY") == "USER_MEMORY":
                    a = v
            if a is None:
                return None
            a.status = AlarmStatus.CANCELLED
            self.alarms[a.alarm_id] = a
        self.event_bus.publish(Event(
            event_type=EventType.ALARM_CANCELLED,
            data={"alarm_id": a.alarm_id},
            source="game_alarm"
        ))
        return a

    def get_alarm_by_id(self, alarm_id: str) -> Alarm | None:
        """按 ID 获取提醒"""
        with self._lock:
            return self.alarms.get(alarm_id)

    def update_objective_state(self, objective_type: str, state: dict[str, Any]):
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
            data={"objective_type": objective_type, "source": "SYSTEM"},
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
        """游戏开始 -> 重置（只清 game 基准提醒；wall 日常提醒跨对局保留，避免进游戏瞬间误清）"""
        with self._lock:
            for alarm in self.alarms.values():
                if alarm.time_base == "game":
                    alarm.status = AlarmStatus.EXPIRED
        self._current_game_time = 0.0
        self._objective_states.clear()

    def _on_game_end(self, event: Event):
        """游戏结束 -> 清理（game 基准提醒过期；wall 基准的日常提醒保留，由 wall 线程继续驱动）"""
        with self._lock:
            for alarm in self.alarms.values():
                if alarm.time_base == "game":
                    alarm.status = AlarmStatus.EXPIRED

    # ===== 内部方法 =====

    def _fire_triggers(self, alarms: list[Alarm]) -> None:
        """把到期提醒置为 TRIGGERED 并发 ALARM_TRIGGERED（now 按各 alarm 基准取值）。"""
        for alarm in alarms:
            alarm.status = AlarmStatus.TRIGGERED
            alarm.triggered_at = time.time()
            now = time.monotonic() if alarm.time_base == "wall" else self._current_game_time
            remaining = alarm.remaining(now)
            self.event_bus.publish(Event(
                event_type=EventType.ALARM_TRIGGERED,
                data={
                    "alarm": self._alarm_to_dict(alarm),
                    "remaining": remaining,
                    "game_time": now,      # wall 时传 wall 现值（字段名保持，语义=基准时间）
                },
                source="game_alarm"
            ))

    def _check_wall_alarms(self) -> None:
        """只检查 wall 基准提醒（wall 线程专用）。

        不碰 game 基准提醒：wall 线程用 1s 轮询驱动，若连带检查 game alarm，
        会用 stale 的 _current_game_time 判断，可能误触发"对局结束后本不该触发的对局内提醒"。
        """
        to_trigger = []
        to_expire = []
        with self._lock:
            for alarm in self.alarms.values():
                if alarm.status != AlarmStatus.ACTIVE or alarm.time_base != "wall":
                    continue
                now = time.monotonic()
                if alarm.should_trigger(now):
                    to_trigger.append(alarm)
                elif alarm.is_expired(now):
                    to_expire.append(alarm)
        self._fire_triggers(to_trigger)
        for alarm in to_expire:
            alarm.status = AlarmStatus.EXPIRED
        self._update_state()

    def _wall_loop(self) -> None:
        """wall 基准（现实时间）提醒驱动：1s 轮询，仅当存在 wall alarm 时检查。

        非对局时 gameTime 不更新 → GAME_TIME_UPDATE 事件不来，
        wall alarm 由本线程独立驱动（喝水等日常提醒不依赖对局）。
        """
        while self._running:
            time.sleep(1.0)
            try:
                with self._lock:
                    has_wall = any(
                        a.time_base == "wall" and a.status == AlarmStatus.ACTIVE
                        for a in self.alarms.values()
                    )
                if has_wall:
                    self._check_wall_alarms()
            except Exception as e:
                print(f"[GameAlarm] wall 轮询异常: {e}")

    def _check_alarms(self, game_time: float):
        """检查所有提醒，触发到期的"""
        to_trigger = []
        to_expire = []

        with self._lock:
            for alarm in self.alarms.values():
                if alarm.status != AlarmStatus.ACTIVE:
                    continue

                now = time.monotonic() if alarm.time_base == "wall" else game_time
                if alarm.should_trigger(now):
                    to_trigger.append(alarm)
                elif alarm.is_expired(now):
                    to_expire.append(alarm)

        # 触发提醒
        self._fire_triggers(to_trigger)

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

    def _alarm_to_dict(self, alarm: Alarm) -> dict[str, Any]:
        """提醒转字典"""
        return {
            "alarm_id": alarm.alarm_id,
            "type": alarm.alarm_type.value,
            "title": alarm.title,
            "game_time_start": alarm.game_time_start,
            "duration": alarm.duration,
            "game_time_end": alarm.game_time_end,
            "time_base": alarm.time_base,
            "lead_time": alarm.lead_time,
            "status": alarm.status.value,
            "remaining": alarm.remaining(
                time.monotonic() if alarm.time_base == "wall" else self._current_game_time),
            "data": alarm.data,
        }
