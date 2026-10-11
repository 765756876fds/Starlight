"""连招执行引擎：按序列发键、按 delay_after_ms 延时。

- key_sender 可注入（默认 keyboard.press_and_release），测试用 fake 替代
- is_executing 线程安全（Lock），执行期间拒绝并发执行——自我触发屏蔽第一道闸
- 只做步骤级保险：不做整连招重试（无完成反馈信号）
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from .models import Combo

KeySender = Callable[[str], None]


def default_key_sender(key: str) -> None:
    """默认按键发送：keyboard 库模拟按下并释放（Windows 需管理员权限）。"""
    import keyboard  # noqa: PLC0415  (延迟导入：仅真实执行时依赖)
    keyboard.press_and_release(key)


class ComboExecutor:
    def __init__(self, key_sender: Optional[KeySender] = None):
        self._key_sender: KeySender = key_sender or default_key_sender
        self._lock = threading.Lock()
        self._executing = False

    @property
    def is_executing(self) -> bool:
        """是否正在执行连招（自我触发屏蔽检查点）。"""
        return self._executing

    def run(self, combo: Combo) -> bool:
        """按顺序发送按键序列。执行期间置 is_executing=True（防自我触发）。

        返回 True=完整执行；False=已在执行中（拒绝并发）。
        """
        if self._executing:
            return False
        with self._lock:
            if self._executing:
                return False
            self._executing = True
        try:
            for step in combo.sequence:
                self._key_sender(step.key)
                delay = int(step.delay_after_ms or 0)
                if delay > 0:
                    time.sleep(delay / 1000.0)
            return True
        finally:
            self._executing = False
