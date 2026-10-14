"""全局键盘监听：keyboard.hook 回调（在 keyboard 库线程串行执行）。

只转发"按键按下"事件、字母/数字等可触发键；修饰键（ctrl/alt/shift/win）
等无意义键直接忽略。真正的自我触发屏蔽在 ComboManager（is_executing +
执行后 grace 窗口），本层只做基础过滤。

watchdog：Windows 低级键盘钩子回调超 300ms 会被静默移除，且 keyboard 库
不自动重连——本层内置看门狗，零回调超 20s 判定掉线后自动重新 hook
（幂等），连续 3 次失败停止，故障不冒泡。
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

# 无意义的修饰键/系统键，不参与 trigger 匹配
_SKIP_KEYS = {
    "ctrl", "alt", "alt gr", "shift", "windows", "cmd", "left windows",
    "right windows", "caps lock", "tab", "esc", "enter", "space",
    "backspace", "delete", "insert", "home", "end", "page up", "page down",
    "up", "down", "left", "right", "print screen", "scroll lock", "pause",
    "num lock", "menu", "apps",
}


class ComboListener:
    def __init__(self, on_key: Callable[[str], None]):
        self._on_key = on_key
        self._hooked = False
        # keyboard.hook() 返回的是"取消函数"（remove 闭包），不是 handle。
        # 依据：keyboard 0.13.5 __init__.py:468 `return remove_`。
        # 存它并在 stop() 用 keyboard.unhook(它) 精确取消自己的 hook。
        self._remove_hook = None
        # 看门狗状态：任何按键回调都刷新 _last_cb_ts，证明 hook 存活
        self._started = False
        self._last_cb_ts = 0.0
        self._stop_watch = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None
        self._reconnects = 0
        self._MAX_RECONNECTS = 3   # 连续重连上限，防循环
        # 零回调超 20s 判定 hook 可能掉线。20s 是"掉线后无反应窗口"的平衡：
        # 太短频繁误判空闲重连，太长掉线窗口久；20s 阈值 + 5s 轮询 = 最坏 25s 发现掉线。
        self._WATCH_IDLE_S = 20.0
        self._WATCH_INTERVAL = 5.0 # 看门狗轮询间隔

    def start(self) -> None:
        if self._hooked:
            return
        try:
            import keyboard  # noqa: PLC0415
            self._remove_hook = keyboard.hook(self._callback)
            self._hooked = True
            print("[Combo] 全局键盘监听已启动（需要管理员权限才能模拟按键）")
            self._started = True
            self._last_cb_ts = time.monotonic()
            self._stop_watch.clear()
            self._watch_thread = threading.Thread(
                target=self._watchdog, daemon=True, name="combo-listener-watchdog")
            self._watch_thread.start()
        except Exception as e:
            self._remove_hook = None
            print(f"[Combo] 键盘监听启动失败: {e}")

    def stop(self) -> None:
        if not self._hooked:
            return
        try:
            import keyboard  # noqa: PLC0415
            if self._remove_hook is not None:
                # 只取消自己的 hook，不清全局。keyboard.unhook 接受 hook() 的
                # 返回值（__init__.py:518-523，实现 _hooks[remove]()，remove_ 闭包
                # 会从 _listener.handlers 移除原 callback 并清理 _hooks 条目）。
                keyboard.unhook(self._remove_hook)
        except Exception:
            pass
        finally:
            self._started = False
            self._stop_watch.set()
            if self._watch_thread and self._watch_thread.is_alive():
                self._watch_thread.join(timeout=1.0)
            self._remove_hook = None
            self._hooked = False

    def _callback(self, event) -> None:
        self._last_cb_ts = time.monotonic()  # 任何按键都证明 hook 存活
        try:
            if getattr(event, "event_type", None) != "down":
                return
            key = getattr(event, "name", None)
            if not key:
                return
            key = str(key).strip().lower()
            if key in _SKIP_KEYS:
                return
            self._on_key(key)
        except Exception as e:
            print(f"[Combo] 按键处理异常: {e}")

    def _watchdog(self) -> None:
        """hook 存活看门狗：零回调超 _WATCH_IDLE_S 判定掉线 → unhook + 重新 hook（幂等）。

        连续 _MAX_RECONNECTS 次失败停止（防循环）；所有异常就地捕获，不影响其他模块。
        """
        while not self._stop_watch.wait(self._WATCH_INTERVAL):
            if not self._started or not self._hooked:
                continue
            idle = time.monotonic() - self._last_cb_ts
            if idle < self._WATCH_IDLE_S:
                continue
            if self._reconnects >= self._MAX_RECONNECTS:
                print("[Combo] 键盘监听重连达上限，停止自动重连")
                break
            self._reconnects += 1
            print(f"[Combo] 键盘监听疑似掉线（{int(idle)}s 无回调），自动重连 #{self._reconnects}")
            try:
                import keyboard  # noqa: PLC0415
                if self._remove_hook is not None:
                    try:
                        keyboard.unhook(self._remove_hook)
                    except Exception:
                        pass
                self._remove_hook = keyboard.hook(self._callback)
                self._hooked = True
                self._last_cb_ts = time.monotonic()
                print("[Combo] 键盘监听已重连")
            except Exception as e:
                print(f"[Combo] 键盘监听重连失败: {e}")
