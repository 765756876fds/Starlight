"""腾讯服 SGP server 自动发现（源码优先，复用 LeagueAkari 原理）。

LeagueAkari: getSgpServerId(auth.region, auth.rsoPlatformId) -> sgp server。
我们最小复用：
  GET /lol-rso-auth/v1/authorization -> currentPlatformId (如 HN10 / GZ100 / CQ100)
  -> base = 腾讯 SGP 域名表（LeagueAkari builtin 配置 2026-07-18）
  -> sub_id = platformId 大写
不能简单拼 {platformId}-sgp.lol.qq.com：HN1/HN10/BGP2 实际域名带 -k8s- 段，
拼法只对 TJ/NJ/GZ/CQ 等区有效，HN10(黑色玫瑰) 会 DNS 解析失败。
"""
from __future__ import annotations

from typing import Callable, Optional, Tuple

RESOLVED = "RESOLVED"
UNSUPPORTED = "UNSUPPORTED"
ERROR = "ERROR"

# LeagueAkari BUILTIN_SGP_LEAGUE_SERVERS_CONFIG（腾讯服部分，2026-07-18）
_TENCENT_SGP_BASES: dict = {
    "HN1": "https://hn1-k8s-sgp.lol.qq.com:21019",
    "HN10": "https://hn10-k8s-sgp.lol.qq.com:21019",
    "TJ100": "https://tj100-sgp.lol.qq.com:21019",
    "TJ101": "https://tj101-sgp.lol.qq.com:21019",
    "NJ100": "https://nj100-sgp.lol.qq.com:21019",
    "GZ100": "https://gz100-sgp.lol.qq.com:21019",
    "CQ100": "https://cq100-sgp.lol.qq.com:21019",
    "BGP2": "https://bgp2-k8s-sgp.lol.qq.com:21019",
    "PBE": "https://pbe-sgp.lol.qq.com:21019",
    "PREPBE": "https://prepbe-sgp.lol.qq.com:21019",
}


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
        key = pid.upper()
        base = _TENCENT_SGP_BASES.get(key)
        if not base:
            # 未知平台：回退原拼法（保持非腾讯区/新大区行为不回归）
            low = pid.lower()
            base = f"https://{low}-sgp.lol.qq.com:21019"
        return RESOLVED, base, key
