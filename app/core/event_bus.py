"""
事件总线 - 整个系统的核心通信机制

所有模块通过事件通信，避免模块间直接依赖。
2999 -> StateDiff -> Event -> EventBus -> GameAlarm / UI / SpeechJudge
"""
import asyncio
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional


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
    MUSIC_PREV = "music.prev"
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
    LCU_CHAMP_SELECT_CHANGED = "lcu.champ_select_changed"
    LCU_READY_CHECK = "lcu.ready_check"

    # ===== 玩家事件 =====
    PLAYER_RESOLVED = "player.resolved"
    PLAYER_HISTORY_LOADED = "player.history_loaded"
    PLAYER_PROFILE_UPDATED = "player.profile_updated"

    # ===== 系统事件 =====
    SYSTEM_STARTUP = "system.startup"
    SYSTEM_SHUTDOWN = "system.shutdown"
    SYSTEM_ERROR = "system.error"


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
                print(f"[EventBus] 事件处理异常 {event.event_type} -> {handler.__name__}: {e}")

    def publish_async(self, event: Event):
        """异步发布事件（在新线程中分发）"""
        thread = threading.Thread(
            target=self.publish,
            args=(event,),
            daemon=True,
            name=f"event-{event.event_type}"
        )
        thread.start()

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
        """关闭事件总线"""
        self._running = False
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
