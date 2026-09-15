"""
Kill Aggregator - 击杀事件聚合

避免连续播报"击杀 击杀 击杀"。
在一个时间窗口内合并击杀事件，统一播报。
"""
import time
import threading
from typing import Any, Dict, List, Optional

from ..core.event_bus import Event


class KillAggregator:
    """
    击杀事件聚合器

    一个人连续杀多个人时，不逐个播报，而是等待窗口结束后统一播报。
    例如：三杀 -> 只说"三杀"，不说三次"击杀"。
    """

    def __init__(self, window: float = 3.0):
        self.window = window  # 聚合窗口（秒）
        self._pending_kills: List[Event] = []
        self._last_kill_time: float = 0
        self._last_killer: str = ""
        self._kill_streak: int = 0
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()
        self._on_expire_callback = None  # 窗口到期回调

    def add(self, event: Event) -> Optional[Event]:
        """
        添加击杀事件

        Returns:
            如果需要立即播报，返回事件；否则返回 None（等待聚合）
        """
        now = time.time()
        killer = event.get("killer_name", "")
        is_current_player = event.get("is_current_player_kill", False)
        is_current_death = event.get("is_current_player_death", False)

        # 当前玩家的击杀/死亡立即播报（不聚合）
        if is_current_player or is_current_death:
            return event

        with self._lock:
            # 检查是否是同一个人的连续击杀
            if killer == self._last_killer and (now - self._last_kill_time) < self.window:
                self._kill_streak += 1
                self._last_kill_time = now
                self._pending_kills.append(event)

                # 如果达到多杀，立即播报
                if self._kill_streak >= 2:
                    result = self._create_multi_kill_event()
                    self._pending_kills.clear()
                    self._kill_streak = 0
                    self._cancel_timer()
                    return result
                # 重置 timer，等待更多击杀
                self._reset_timer()
                return None
            else:
                # 新的击杀者，先处理之前的待播报
                result = None
                if self._pending_kills:
                    result = self._pending_kills[0]
                    self._pending_kills.clear()
                    self._kill_streak = 0

                # 开始新的聚合
                self._last_killer = killer
                self._last_kill_time = now
                self._kill_streak = 1
                self._pending_kills = [event]

                # 启动 timer，等待窗口结束自动 flush
                self._reset_timer()

                return result

    def flush(self) -> Optional[Event]:
        """
        强制刷新，返回待播报的事件

        在聚合窗口结束后调用。
        """
        with self._lock:
            self._cancel_timer()
            if self._pending_kills:
                if self._kill_streak >= 2:
                    event = self._create_multi_kill_event()
                else:
                    event = self._pending_kills[0]
                self._pending_kills.clear()
                self._kill_streak = 0
                return event
            return None

    def _reset_timer(self):
        """重置聚合窗口 timer"""
        self._cancel_timer()
        self._timer = threading.Timer(self.window, self._on_timer_expired)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        """取消 timer"""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def set_on_expire(self, callback):
        """设置窗口到期回调函数"""
        self._on_expire_callback = callback

    def _on_timer_expired(self):
        """窗口到期，调用回调"""
        if self._on_expire_callback is not None:
            try:
                self._on_expire_callback()
            except Exception:
                pass

    def get_expired_event(self) -> Optional[Event]:
        """获取窗口到期后的待播报事件（由外部调用）"""
        return self.flush()

    def shutdown(self):
        """关闭聚合器，清理 timer"""
        self._cancel_timer()

    def _create_multi_kill_event(self) -> Event:
        """创建多杀事件"""
        if not self._pending_kills:
            return None

        first = self._pending_kills[0]
        return Event(
            event_type="game.multi_kill",
            data={
                "killer_name": self._last_killer,
                "count": self._kill_streak,
                "kills": [e.data for e in self._pending_kills],
                "game_time": first.get("game_time", 0),
            },
            source="kill_aggregator"
        )

    def reset(self):
        """重置状态"""
        self._pending_kills.clear()
        self._last_kill_time = 0
        self._last_killer = ""
        self._kill_streak = 0
