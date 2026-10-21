"""
事件总线 - 整个系统的核心通信机制

所有模块通过事件通信，避免模块间直接依赖。
2999 -> StateDiff -> Event -> EventBus -> GameAlarm / UI / SpeechJudge
"""
import logging
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

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
    data: dict[str, Any] = field(default_factory=dict)
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
    当前按到达顺序 FIFO 分发；未实现优先级队列。如需优先级，请通过订阅顺序或独立通道控制。
    """

    def __init__(self):
        self._subscribers: dict[str, list[Callable]] = defaultdict(list)
        self._wildcard_subscribers: list[Callable] = []
        self._lock = threading.Lock()
        self._event_history: list[Event] = []
        self._max_history = 1000
        self._running = True
        # 状态合并竞态治理：正在执行的 state key + 执行期间同 key 的最新待处理事件
        self._running_state_keys: set[str] = set()
        self._pending_latest_state: dict[str, Event] = {}
        self._is_shutting_down = False
        # ---- 异步事件物理隔离：双池 ----
        # 状态池：高频可丢弃事件（time_update/snapshot 等），按 key 合并保最新、上限 64
        self._state_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="eventbus-state"
        )
        # 待处理状态事件队列（按 key 合并语义：同 key 新替旧，满且无同 key 时丢最旧）
        self._state_queue: deque[Event] = deque()
        # 临界池：不可丢弃事件（击杀/提醒/系统错误等），容量大、拥堵记录但不丢。
        # 关键事件必须保证严格 FIFO 顺序，故限制单 worker；禁止在此池放入长耗时任务。
        self._critical_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="eventbus-critical"
        )
        # 兼容别名（历史测试/调用方按 _async_executor 断言行为）
        self._async_executor = self._critical_executor
        self._pool_lock = threading.Lock()
        self._critical_pending = 0
        self._STATE_MAX_PENDING = 64   # 状态池硬上限：满且无同 key 可合并时，丢弃最早的事件
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

    @staticmethod
    def _state_key(event: Event) -> str:
        """状态池合并 key：优先取事件的实体 key（data.entity_key），否则按事件类型合并。"""
        return str(event.data.get("entity_key") or event.event_type)

    def _submit_state(self, event: Event):
        """状态池：按 key 合并保最新（同 key 新事件替换旧事件，保留最新状态）；
        无同 key 时追加到队尾；达到硬上限（64）且无同 key 可替换时，丢弃最早的事件；
        若 key 正在执行，缓存为最新待处理，等当前执行完立即消费（最终收敛到最新）。"""
        key = self._state_key(event)
        with self._pool_lock:
            # 该 key 正在执行：不丢弃，缓存为最新待处理（放在"同 key 替换队列"之前，
            # 避免执行中的 key 被队列里的旧记录覆盖/混淆）
            if key in self._running_state_keys:
                self._pending_latest_state[key] = event
                return
            for i, pending in enumerate(self._state_queue):
                if self._state_key(pending) == key:
                    # 同 key：新事件替换旧事件（不追加、不重复执行）
                    self._state_queue[i] = event
                    break
            else:
                if len(self._state_queue) >= self._STATE_MAX_PENDING:
                    # 满且无同 key 可合并：丢弃最早的事件腾出空间
                    self._state_queue.popleft()
                self._state_queue.append(event)
        try:
            future = self._state_executor.submit(self._run_state_event, event)
        except RuntimeError:
            # 总线正在关闭，不再接受新事件；避免回退同步阻塞调用方
            with self._pool_lock:
                if event in self._state_queue:
                    self._state_queue.remove(event)
            logger.warning("EventBus 关闭中，事件 %s 被丢弃", event.event_type)
            return
        future.add_done_callback(lambda _f: self._dec_state(event))

    def _run_state_event(self, event: Event):
        """状态池 worker 执行体：登记运行中 key，执行后释放并立即消费缓存的最新状态。"""
        key = self._state_key(event)
        with self._pool_lock:
            self._running_state_keys.add(key)
        try:
            self.publish(event)
        finally:
            with self._pool_lock:
                self._running_state_keys.discard(key)
                latest = self._pending_latest_state.pop(key, None)
            # 执行期间同 key 有更新到达：立即重新入队消费（保证最终收敛到最新）
            if latest is not None:
                self._submit_state(latest)

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
            future = self._critical_executor.submit(self._run_critical_event, event)
        except RuntimeError:
            # 总线正在关闭，不再接受新事件；避免回退同步阻塞调用方
            with self._pool_lock:
                self._critical_pending -= 1
            logger.warning("EventBus 关闭中，事件 %s 被丢弃", event.event_type)
            return
        future.add_done_callback(lambda _f: self._dec_pending("critical"))

    def _run_critical_event(self, event: Event):
        """临界池 worker 执行体：对单 worker 的关键任务做耗时监控。"""
        start_ts = time.monotonic()
        try:
            self.publish(event)
        finally:
            elapsed_ms = (time.monotonic() - start_ts) * 1000
            if elapsed_ms > 1000:
                logger.warning(
                    "[EventBus] 关键任务 %s 耗时 %.0fms，请检查是否误放长任务",
                    event.event_type, elapsed_ms,
                )

    def _dec_state(self, event: Event):
        """状态池事件执行完成：从待处理队列移除（可能已被同 key 新事件替换，忽略即可）。"""
        with self._pool_lock:
            if event in self._state_queue:
                self._state_queue.remove(event)

    def _dec_pending(self, pool: str):
        with self._pool_lock:
            if pool == "critical":
                self._critical_pending -= 1

    def get_history(self, event_type: str | None = None, limit: int = 100) -> list[Event]:
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

    def shutdown(self, timeout: float = 3.0):
        """
        关闭事件总线（有界等待）

        先拒绝新事件，取消未开始的排队任务；对已开始执行的任务做有界等待
        （timeout 秒），超时后记录日志并放弃等待，避免无限阻塞退出流程。
        """
        self._is_shutting_down = True
        self._running = False
        try:
            pending = -1
            q = getattr(self._critical_executor, '_work_queue', None)
            if q is not None:
                pending = q.qsize()
            if pending > 0:
                logger.info("[EventBus] 关闭前有 %d 个关键任务待完成", pending)
        except Exception:
            pass
        try:
            self._state_executor.shutdown(wait=False, cancel_futures=True)
            self._critical_executor.shutdown(wait=False, cancel_futures=True)
        except Exception as e:
            logger.warning("[EventBus] executor shutdown 异常: %s", e)
        # 有界等待：轮询是否还有未完成（执行中/排队）任务
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if not self._has_running_tasks():
                break
            time.sleep(0.1)
        else:
            logger.warning("[EventBus] 关闭超时（%ss），部分任务被强制放弃", timeout)
        with self._lock:
            self._subscribers.clear()
            self._wildcard_subscribers.clear()
        with self._pool_lock:
            self._state_queue.clear()
            self._pending_latest_state.clear()
            self._running_state_keys.clear()
        logger.info("[EventBus] shutdown 完成")

    def _has_running_tasks(self) -> bool:
        """检测双池是否仍有未完成任务（unfinished_tasks：排队 + 执行中都计入）。"""
        try:
            for ex in (self._state_executor, self._critical_executor):
                q = getattr(ex, '_work_queue', None)
                if q is not None and q.unfinished_tasks > 0:
                    return True
            return False
        except Exception:
            return False


# 全局事件总线单例
_event_bus: EventBus | None = None


def get_event_bus() -> EventBus:
    """获取全局事件总线单例"""
    global _event_bus
    if _event_bus is None:
        _event_bus = EventBus()
    return _event_bus
