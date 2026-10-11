"""全局键盘监听：keyboard.hook 回调（在 keyboard 库线程串行执行）。

只转发"按键按下"事件、字母/数字等可触发键；修饰键（ctrl/alt/shift/win）
等无意义键直接忽略。真正的自我触发屏蔽在 ComboManager（is_executing +
执行后 grace 窗口），本层只做基础过滤。
"""
from __future__ import annotations

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

    def start(self) -> None:
        if self._hooked:
            return
        try:
            import keyboard  # noqa: PLC0415
            self._remove_hook = keyboard.hook(self._callback)
            self._hooked = True
            print("[Combo] 全局键盘监听已启动（需要管理员权限才能模拟按键）")
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
            self._remove_hook = None
            self._hooked = False

    def _callback(self, event) -> None:
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
