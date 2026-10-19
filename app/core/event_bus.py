"""
事件总线 - 整个系统的核心通信机制

所有模块通过事件通信，避免模块间直接依赖。
2999 -> StateDiff -> Event -> EventBus -> GameAlarm / UI / SpeechJudge
"""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class EventPriority(Enum):
    """事件优先级"""
    HIGH = 0
    NORMAL = 1
    LOW = 2


@dataclass
class Event:
    """通用事件"""
    event_type: str  # 事件类型，如 "game.dragon_killed", "music.next"
    data: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    priority: EventPriority = EventPriority.NORMAL
    source: str = ""  # 事件来源模块

    def __post_init__(self):
        if "timestamp" not in self.data:
            self.data["timestamp"] = self.timestamp

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)


# 事件类型常量
class EventType:
    # ===== 音乐事件 =====
    MUSIC_PLAY = "music.play"
    MUSIC_PAUSE = "music.pause"
    MUSIC_RESUME = "music.resume"
    MUSIC_NEXT = "music.next"
    MUSIC_PREV = "music.previous"
    MUSIC_VOLUME = "music.volume"
    MUSIC_SEEK = "music.seek"
    MUSIC_SEARCH = "music.search"
    MUSIC_HIGHLIGHT = "music.highlight"
    MUSIC_TRACK_CHANGED = "music.track_changed"
    MUSIC_STATE_CHANGED = "music.state_changed"

    # ===== LoL 游戏事件 =====
    GAME_STARTED = "game.started"
    GAME_ENDED = "game.ended"
    GAME_TIME_UPDATE = "game.time_update"
    PLAYER_LEVEL_UP = "game.player_level_up"
    CHAMPION_KILL = "game.champion_kill"
    MULTI_KILL = "game.multi_kill"
    FIRST_BLOOD = "game.first_blood"
    ACE = "game.ace"
    TOWER_DESTROYED = "game.tower_destroyed"
    INHIBITOR_DESTROYED = "game.inhibitor_destroyed"
    DRAGON_KILLED = "game.dragon_killed"
    BARON_KILLED = "game.baron_killed"
    HERALD_KILLED = "game.herald_killed"
    OBJECTIVE_SPAWN = "game.objective_spawn"
    ITEM_CHANGED = "game.item_changed"
    PLAYER_ITEM_CHANGED = "game.item_changed"  # 别名
    SPELL_CHANGED = "game.spell_changed"
    PLAYER_SUMMONER_SPELL_USED = "game.player_summoner_spell_used"
    GAME_SNAPSHOT = "game.snapshot"

    # ===== GameAlarm 事件 =====
    ALARM_CREATED = "alarm.created"
    ALARM_TRIGGERED = "alarm.triggered"
    ALARM_CANCELLED = "alarm.cancelled"
    FLASH_RECORDED = "alarm.flash_recorded"
    WARD_RECORDED = "alarm.ward_recorded"

    # ===== 语音事件 =====
    VOICE_TEXT_RECEIVED = "voice.text_received"
    VOICE_INTENT_PARSED = "voice.intent_parsed"
    VOICE_COMMAND_EXECUTED = "voice.command_executed"

    # ===== 语音输出事件 =====
    SPEECH_SAY = "speech.say"
    SPEECH_STARTED = "speech.started"
    SPEECH_FINISHED = "speech.finished"

    # ===== LCU 事件 =====
    LCU_CONNECTED = "lcu.connected"
    LCU_DISCONNECTED = "lcu.disconnected"
    LCU_GAMEFLOW_CHANGED = "lcu.gameflow_changed"
    LCU_SUMMONER_UPDATED = "lcu.summoner_updated"
    LCU_CHAMP_SELECT_CHANGED = "lcu.champ_select_changed"
    LCU_READY_CHECK = "lcu.ready_check"

    # ===== 玩家事件 =====
    PLAYER_RESOLVED = "player.resolved"
    PLAYER_HISTORY_LOADED = "player.history_loaded"
    PLAYER_PROFILE_UPDATED = "player.profile_updated"

    # ===== Cooldown 事件 =====
    COOLDOWN_STARTED = "cooldown.started"
    COOLDOWN_REMIND = "cooldown.remind"
    COOLDOWN_CANCELLED = "cooldown.cancelled"
    COOLDOWN_ADJUSTMENT = "cooldown.adjustment"  # 动态修正事件

    # ===== 系统事件 =====
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"


# 高频状态事件：可合并、可丢弃（拥堵时丢新保旧，最新值才重要）
_STATE_POOL_EVENTS = frozenset({
    EventType.GAME_TIME_UPDATE,
    EventType.GAME_SNAPSHOT,
    EventType.MUSIC_STATE_CHANGED,
})


class EventBus:
    """
    线程安全的事件总线

    支持同步和异步订阅者。
    所有事件按优先级排序后分发。
    """

    def __init__(self):
        self._subscribers: Dict[str, List[Callable]] = defaultdict(list)
        self._wildcard_subscribers: List[Callable] = []
        self._lock = threading.Lock()
        self._event_history: List[Event] = []
        self._max_history = 1000
        self._running = True
        # ---- 异步事件物理隔离：双池 ----
        # 状态池：高频可丢弃事件（time_update/snapshot 等），容量小、拥堵丢新
        self._state_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="eventbus-state"
        )
        # 临界池：不可丢弃事件（击杀/提醒/系统错误等），容量大、拥堵记录但不丢
        self._critical_executor = ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="eventbus-critical"
        )
        # 兼容别名（历史测试/调用方按 _async_executor 断言行为）
        self._async_executor = self._critical_executor
        self._pool_lock = threading.Lock()
        self._state_pending = 0
        self._critical_pending = 0
        self._STATE_MAX_PENDING = 4   # 状态池拥堵阈值：超则丢弃新事件（合并语义）
        self._CRITICAL_MAX_PENDING = 32  # 临界池告警阈值：超则记录 ERROR，绝不丢弃

    def subscribe(self, event_type: str, handler: Callable[[Event], None]):
        """订阅特定事件类型"""
        with self._lock:
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def subscribe_all(self, handler: Callable[[Event], None]):
        """订阅所有事件（通配符）"""
        with self._lock:
            if handler not in self._wildcard_subscribers:
                self._wildcard_subscribers.append(handler)

    def unsubscribe(self, event_type: str, handler: Callable):
        """取消订阅"""
        with self._lock:
            if handler in self._subscribers.get(event_type, []):
                self._subscribers[event_type].remove(handler)

    def publish(self, event: Event):
        """
        发布事件（同步分发）
        异常被捕获并记录，不影响其他订阅者
        """
        if not self._running:
            logger.warning("EventBus 已关闭，丢弃事件 %s", event.event_type)
            return

        # 记录历史
        with self._lock:
            self._event_history.append(event)
            if len(self._event_history) > self._max_history:
                self._event_history.pop(0)

        # 收集所有处理函数
        handlers = []
        with self._lock:
            handlers.extend(self._subscribers.get(event.event_type, []))
            handlers.extend(self._wildcard_subscribers)

        # 按顺序执行
        for handler in handlers:
            try:
                handler(event)
            except Exception as e:
                handler_name = getattr(handler, "__name__", "?")
                # X 方案双写：print → UI + print-{date}.log；logger.error → app.log（结构化）
                print(f"[EventBus] 事件处理异常 {event.event_type} -> {handler_name}: {e}")
                logger.error("事件处理异常 %s -> %s: %s", event.event_type, handler_name, e)
                # SYSTEM_ERROR 分发自身失败：不嵌套发布，避免无限递归
                if event.event_type == EventType.SYSTEM_ERROR:
                    continue
                try:
                    self.publish(Event(
                        event_type=EventType.SYSTEM_ERROR,
                        data={
                            "source_event_type": event.event_type,
                            "source": event.source,
                            "error_type": type(e).__name__,
                            "error_msg": str(e),
                        },
                        source="event_bus",
                    ))
                except Exception:
                    logger.error("发布 SYSTEM_ERROR 失败（源事件 %s）", event.event_type, exc_info=True)

    def publish_async(self, event: Event):
        """异步发布事件（按事件类型路由到隔离双池）"""
        if not self._running:
            logger.warning("EventBus 已关闭，丢弃异步事件 %s", event.event_type)
            return
        if event.event_type in _STATE_POOL_EVENTS:
            self._submit_state(event)
        else:
            self._submit_critical(event)

    def _submit_state(self, event: Event):
        """状态池：拥堵时丢弃新事件（高频状态合并语义，最新值才重要）。"""
        with self._pool_lock:
            if self._state_pending >= self._STATE_MAX_PENDING:
                logger.warning(
                    "状态事件池拥堵，丢弃事件 %s（pending=%d）",
                    event.event_type, self._state_pending,
                )
                return
            self._state_pending += 1
        try:
            future = self._state_executor.submit(self.publish, event)
        except RuntimeError:
            with self._pool_lock:
                self._state_pending -= 1
            self.publish(event)  # shutdown 竞态兜底
            return
        future.add_done_callback(lambda _f: self._dec_pending("state"))

    def _submit_critical(self, event: Event):
        """临界池：容量足够大，拥堵时记录 ERROR 但绝不静默丢弃。"""
        with self._pool_lock:
            if self._critical_pending >= self._CRITICAL_MAX_PENDING:
                logger.error(
                    "临界事件池拥堵（pending=%d），事件 %s 已提交但可能延迟",
                    self._critical_pending, event.event_type,
                )
            self._critical_pending += 1
        try:
            future = self._critical_executor.submit(self.publish, event)
        except RuntimeError:
            with self._pool_lock:
                self._critical_pending -= 1
            self.publish(event)  # shutdown 竞态兜底
            return
        future.add_done_callback(lambda _f: self._dec_pending("critical"))

    def _dec_pending(self, pool: str):
        with self._pool_lock:
            if pool == "state":
                self._state_pending -= 1
            else:
                self._critical_pending -= 1

    def get_history(self, event_type: Optional[str] = None, limit: int = 100) -> List[Event]:
        """获取事件历史"""
        with self._lock:
            history = list(self._event_history)
        if event_type:
            history = [e for e in history if e.event_type == event_type]
        return history[-limit:]

    def clear_history(self):
        """清空事件历史"""
        with self._lock:
            self._event_history.clear()

    def shutdown(self):
        """
        关闭事件总线（优雅关闭）

        先拒绝新事件，再等待已提交的异步 handler 全部执行完毕
        （wait=True）。注意：订阅者 handler 应避免无限阻塞，否则退出
        流程会一直等待。
        """
        self._running = False
        self._state_executor.shutdown(wait=True, cancel_futures=True)
        self._critical_executor.shutdown(wait=True, cancel_futures=True)
        with self._lock:
            self._subscribers.clear()
            self._wildcard_subscribers.clear()


# 全局事件总线单例
_event_bus: Optional[EventBus] = None


def get_event_bus() -> EventBus:
    """获取全局事件总线单例"""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
