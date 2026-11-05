"""Bounded user notifications. Business and speech outcomes are independent.

No Qt, logging interception, disk history or business execution lives here.
An operation keeps one id across synchronous calls and background commands.
"""
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
import logging
import re
import threading
import time
import uuid

from .operation_result import OperationResult, OperationStatus

logger = logging.getLogger(__name__)
_operation = ContextVar("notification_operation", default="")


def current_operation_id():
    return _operation.get()


@contextmanager
def operation_scope(operation_id):
    token = _operation.set(operation_id or "")
    try:
        yield
    finally:
        _operation.reset(token)


def user_message(text, fallback="操作没有完成，请重试或查看设置中的诊断结果"):
    text = str(text or "").strip()
    if any(part in text.lower() for part in (
        "traceback", "intent(", "commandbus", "handler", "params=", "confidence",
        "conf=", "ipc", "exception", "error:", "thread-", "0xc000", "127.0.0.1",
        "token=", "api_key", "authorization", "cookie", "bearer ")):
        return fallback
    if re.search(r"[A-Za-z]:[\\/]|/(?:home|users|tmp)/", text):
        return fallback
    for before, after in (("控制器", "功能"), ("会话标识", "播放状态"),
                          ("music 快照", "音乐状态"), ("movie 快照", "电影状态"),
                          ("music 已", "音乐已"), ("movie 已", "电影已")):
        text = text.replace(before, after)
    return text[:300] or fallback


@dataclass(frozen=True)
class Notification:
    id: str
    title: str
    message: str = ""
    status: str = "processing"
    source: str = "app"
    heard: str = ""
    advice: str = ""
    phase: str = "running"
    speech_status: str = "none"
    created_at: float = 0.0
    updated_at: float = 0.0


class NotificationCenter:
    def __init__(self, limit=200, clock=time.time):
        self.limit = max(1, limit)
        self._clock = clock
        self._lock = threading.RLock()
        self._items = OrderedDict()
        self._listeners = set()
        self._dedup = {}
        self.current_voice_id = ""

    def subscribe(self, callback):
        with self._lock:
            self._listeners.add(callback)
        return lambda: self.unsubscribe(callback)

    def unsubscribe(self, callback):
        with self._lock:
            self._listeners.discard(callback)

    def _emit(self):
        with self._lock:
            listeners = tuple(self._listeners)
        for callback in listeners:
            try:
                callback()
            except Exception:
                logger.debug("notification listener unavailable", exc_info=True)

    def post(self, title, message="", *, status="processing", source="app",
             operation_id=None, heard="", advice="", phase="running", dedup_key=None):
        now = self._clock()
        with self._lock:
            if dedup_key:
                self._dedup = {k: v for k, v in self._dedup.items() if now - v[1] < 8}
                previous = self._dedup.get(dedup_key)
                if previous and previous[0] in self._items:
                    return previous[0]
            operation_id = operation_id or uuid.uuid4().hex
            if operation_id in self._items:
                raise ValueError("operation already exists; use update")
            self._items[operation_id] = Notification(
                operation_id, user_message(title), user_message(message) if message else "", status,
                source, str(heard or "")[:200], user_message(advice) if advice else "", phase,
                created_at=now, updated_at=now)
            if source == "voice":
                self.current_voice_id = operation_id
            if dedup_key:
                self._dedup[dedup_key] = (operation_id, now)
            while len(self._items) > self.limit:
                self._items.popitem(last=False)
        self._emit()
        return operation_id

    def update(self, operation_id, **changes):
        # Never resurrect cleared/evicted operations when a late result arrives.
        with self._lock:
            old = self._items.get(operation_id)
            if old is None:
                return False
            for field in ("title", "message", "advice"):
                if field in changes:
                    changes[field] = user_message(changes[field]) if changes[field] else ""
            if "heard" in changes:
                changes["heard"] = str(changes["heard"] or "")[:200]
            if all(getattr(old, k) == v for k, v in changes.items()):
                return True
            self._items[operation_id] = replace(old, **changes, updated_at=self._clock())
        self._emit()
        return True

    def get(self, operation_id):
        with self._lock:
            return self._items.get(operation_id)

    def focus(self, operation_id):
        """Promote an input-device state, without changing history ordering."""
        with self._lock:
            if operation_id not in self._items:
                return
            self.current_voice_id = operation_id
        self._emit()

    def history(self):
        with self._lock:
            return tuple(reversed(self._items.values()))

    def clear(self):
        with self._lock:
            self._items.clear()
            self._dedup.clear()
            self.current_voice_id = ""
        self._emit()


_center = None
_center_lock = threading.Lock()


def get_notification_center():
    global _center
    with _center_lock:
        if _center is None:
            _center = NotificationCenter()
        return _center


def notify(title, message="", *, status="success", source="app", operation_id=None,
           advice="", dedup_key=None):
    center = get_notification_center()
    operation_id = operation_id or current_operation_id()
    if operation_id:
        center.update(operation_id, title=title, message=message, status=status,
                      advice=advice, phase="done")
        return operation_id
    return center.post(title, message, status=status, source=source, advice=advice,
                       phase="done", dedup_key=dedup_key)


_ACTIONS = {
    "music.pause": "暂停音乐", "music.resume": "继续音乐", "music.stop": "停止音乐",
    "music.next": "播放下一首", "music.previous": "播放上一首", "music.play": "播放音乐",
    "music.search_play": "搜索并播放歌曲", "music.set_speed": "设置音乐倍速",
    "music.volume_set": "调整音乐音量", "music.volume_up": "调大音乐音量",
    "music.volume_down": "调小音乐音量", "music.identify": "打开识曲",
    "movie.search_play": "搜索并播放电影", "movie.pause": "暂停电影",
    "movie.resume": "继续电影", "movie.stop": "停止电影",
    "cooldown.start": "创建技能计时", "cooldown.cancel": "取消技能计时",
    "cooldown.cancel_all": "取消全部技能计时", "cooldown.list": "查询技能计时",
    "cooldown.query": "查询技能冷却", "system.stop_tts": "停止播报",
}


def action_title(command):
    return _ACTIONS.get(command, "处理这条指令")


def command_result(command, value=None, error=None):
    """Observing a result must never change a completed business operation."""
    try:
        _command_result(command, value, error)
    except Exception:
        logger.exception("notification_result_observer_failed")


def _command_result(command, value=None, error=None):
    """Observe existing return contracts; unknown legacy None stays unknown."""
    operation_id = current_operation_id()
    if not operation_id:
        return
    action = action_title(command)
    if error is not None:
        notify("操作没有完成", "暂时无法完成这次操作", status="failed",
               advice="请重试；持续失败时在设置中查看诊断结果")
        return
    if isinstance(value, OperationResult):
        status = {OperationStatus.CONFIRMED: "success", OperationStatus.ACCEPTED: "warning",
                  OperationStatus.FAILED: "failed"}.get(value.status, "warning")
        notify(value.message, status=status)
    elif command in ("music.pause", "music.resume", "movie.pause", "movie.resume"):
        # VoiceRuntime verifies actual player properties after these calls.
        if value is False:
            notify("操作没有完成", status="failed", advice="请确认媒体已打开，再试一次")
    elif command == "cooldown.list" and isinstance(value, list):
        notify("技能计时查询完成", f"当前有 {len(value)} 个技能计时" if value else "当前没有正在进行的技能计时")
    elif command == "cooldown.cancel_all" and type(value) is int and value >= 0:
        notify("技能计时已清除", f"已取消 {value} 个计时")
    elif command == "cooldown.query" and isinstance(value, dict):
        notify("技能冷却查询完成" if value.get("kind") != "unknown" else "没有找到对应技能",
               value.get("text", ""), status="warning" if value.get("kind") == "unknown" else "success")
    elif value is False or (command == "cooldown.start" and value is None):
        notify(f"{action}未完成", status="failed", advice="请检查当前播放或游戏状态，再试一次")
    elif command == "cooldown.start" and getattr(value, "id", None):
        notify("技能计时已创建")
    elif value is True and command in ("cooldown.start", "cooldown.cancel", "music.set_speed",
                                       "music.sleep_timer_set", "music.sleep_timer_cancel", "music.add_next", "movie.stop"):
        notify({"cooldown.start": "技能计时已创建", "cooldown.cancel": "技能计时已取消",
                "music.set_speed": "音乐倍速已设置", "music.add_next": "已设为下一首播放",
                "movie.stop": "电影已停止"}.get(command, "操作已完成"))
    elif command in ("music.search_play", "movie.search_play") and (isinstance(value, dict) or value is True):
        notify("已找到内容，播放结果待确认", "请在对应的播放页面确认状态", status="warning")
    else:
        notify(f"已请求{action}", "尚未收到完成确认，请查看对应页面", status="warning")
