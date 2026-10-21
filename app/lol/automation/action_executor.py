"""两阶段 PATCH：hover → (可被 stop 打断) → lock。

参考 Syronss bot.py 的安全设计：
  PATCH {championId}
  stop_event.wait(0.15)
  PATCH {championId, completed: True}
"""
from __future__ import annotations

import threading
from collections.abc import Callable

# 阶段 1 → 阶段 2 之间的停顿（秒）。可被 stop_event 打断。
HOVER_TO_LOCK_DELAY = 0.15


class ChampSelectActionExecutor:
    """对 LCU champ-select action 的两阶段 PATCH 封装。"""

    def __init__(
        self,
        request_with_status: Callable[[str, str, dict | None], tuple],
        stop_event: threading.Event,
        on_event: Callable[[str, dict], None] | None = None,
    ):
        self._request = request_with_status
        self._stop = stop_event
        self._on_event = on_event or (lambda code, payload: None)

    def _post(self, method: str, endpoint: str, body: dict | None = None) -> int:
        status, _ = self._request(method, endpoint, body)
        return int(status or 0)

    def two_phase_patch(
        self,
        action_id: int,
        champion_id: int,
        *,
        hover_only: bool = False,
    ) -> bool:
        """执行两阶段 PATCH。

        hover_only=True 时只做阶段 1（预选/亮），不做阶段 2（锁定）。
        返回 True 表示至少阶段 1 成功；stop 在阶段 1 后触发时返回 False。
        """
        endpoint = f"/lol-champ-select/v1/session/actions/{action_id}"

        # 阶段 1：hover
        status1 = self._post("PATCH", endpoint, {"championId": champion_id})
        if status1 not in (200, 204):
            return False

        if hover_only:
            return True

        # 阶段 1.5：stop 检查
        if self._stop.wait(HOVER_TO_LOCK_DELAY):
            return False

        # 阶段 2：lock
        status2 = self._post(
            "PATCH", endpoint, {"championId": champion_id, "completed": True}
        )
        return status2 in (200, 204)
