"""Feature 基类：每个自动化功能独立、可单独开关、单独观察。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional


@dataclass
class FeatureResult:
    """一个 tick 的结果。"""
    action: Optional[str] = None        # None | 'post' | 'patch'
    endpoint: str = ""
    status_code: int = 0
    detail: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


class Feature:
    """所有自动化 feature 的基类。"""
    name: str = "feature"

    def __init__(self, enabled: bool = False):
        self.enabled = enabled
        self._stats: Dict[str, Any] = {
            "ticks": 0,
            "actions": 0,
            "errors": 0,
        }

    def on_state_change(self, old_state: str, new_state: str, session: Optional[dict]) -> None:
        """gameflow 状态变化时调用（可选钩子）。"""

    def tick(self, session: Any, config: Any) -> FeatureResult:
        """每个 polling tick 调用。session 是上下文（gameflow phase 等）。"""
        self._stats["ticks"] += 1
        return FeatureResult()

    def stop(self) -> None:
        """紧急停止。"""

    def status(self) -> Dict[str, Any]:
        return {"name": self.name, "enabled": self.enabled, **self._stats}

    def report(self) -> Dict[str, Any]:
        return self.status()

    # 内部统计帮助
    def _record_action(self, result: FeatureResult) -> FeatureResult:
        self._stats["actions"] += 1
        return result

    def _record_error(self, result: FeatureResult) -> FeatureResult:
        self._stats["errors"] += 1
        return result
