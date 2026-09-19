"""腾讯服 SGP server 自动发现（源码优先，复用 LeagueAkari 原理）。

LeagueAkari: getSgpServerId(auth.region, auth.rsoPlatformId) -> sgp server。
我们最小复用：
  GET /lol-rso-auth/v1/authorization -> currentPlatformId (如 HN10 / GZ100 / CQ100)
  -> base = https://{platformId小写}-sgp.lol.qq.com:21019
  -> sub_id = platformId 大写
不硬编码大区表，按平台 ID 推导。
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

RESOLVED = "RESOLVED"
UNSUPPORTED = "UNSUPPORTED"
ERROR = "ERROR"


class SgpServerResolver:
    def __init__(self, lcu_get: Callable[[str], Optional[dict]]):
        self._lcu = lcu_get

    def resolve(self) -> Tuple[str, Optional[str], Optional[str]]:
        """返回 (state, base_url, sub_id)。"""
        try:
            auth = self._lcu("/lol-rso-auth/v1/authorization")
        except Exception:
            return ERROR, None, None
        if not isinstance(auth, dict):
            return ERROR, None, None
        pid = auth.get("currentPlatformId") or ""
        if not pid:
            return UNSUPPORTED, None, None
        low = pid.lower()
        base = f"https://{low}-sgp.lol.qq.com:21019"
        return RESOLVED, base, pid
