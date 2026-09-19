"""SGP 战绩 Provider（迁移自 LeagueAkari match-history-query.ts）。

LeagueAkari 真实调用链（源码考古）：
  GET {SGP_BASE}/match-history-query/v1/products/lol/player/{puuid}/SUMMARY
      ?startIndex=N&count=M
      headers: Authorization: Bearer <entitlements-token>
               x-akari-token-type: entitlements
               x-akari-sgp-server-id: <sub-server-id>
  -> { games: [ { metadata, json: { gameId, gameCreation, gameDuration, queueId,
        gameMode, participants: [ ...flat stats incl puuid, win, KDA, gold, cs... ] } } ] }

我们的最小迁移：
- 固定腾讯服个人使用，不搬 LeagueAkari 的跨区/Electron 配置系统；
- entitlements token 与 SGP base 由 LCU 提供（注入可替换，便于测试）；
- SUMMARY 返回的 participants 已是扁平结构，直接按 puuid 取自己那一行。
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Callable, Optional

from .match_model import (
    MatchHistoryResult, MatchRecord,
    AVAILABLE, NO_DATA, AUTH_REQUIRED, SGP_UNAVAILABLE, ERROR,
)


def _extract_own_participant(json_block: dict, puuid: str) -> Optional[dict]:
    for p in json_block.get("participants", []) or []:
        if p.get("puuid") == puuid:
            return p
    return None


def parse_sgp_summary(payload: dict, puuid: str) -> MatchHistoryResult:
    """把 SGP SUMMARY 响应解析成统一 MatchRecord 列表。"""
    if not isinstance(payload, dict):
        return MatchHistoryResult(state=ERROR, message="bad_payload")
    games = payload.get("games") or []
    if not games:
        return MatchHistoryResult(state=NO_DATA, total=0)
    records = []
    for g in games:
        j = g.get("json") or {}
        if not isinstance(j, dict):
            continue
        me = _extract_own_participant(j, puuid)
        if me is None:
            continue  # 找不到自己，不伪造
        cs = (me.get("totalMinionsKilled") or 0) + (me.get("neutralMinionsKilled") or 0)
        records.append(MatchRecord(
            game_id=int(j.get("gameId") or 0),
            game_creation=int(j.get("gameCreation") or 0),
            game_duration=int(j.get("gameDuration") or me.get("timePlayed") or 0),
            queue_id=int(j.get("queueId") or 0),
            champion_id=int(me.get("championId") or 0),
            champion_name=str(me.get("championName") or ""),
            win=bool(me.get("win")) if "win" in me else None,
            kills=me.get("kills"),
            deaths=me.get("deaths"),
            assists=me.get("assists"),
            cs=cs,
            gold=me.get("goldEarned"),
            team_position=str(me.get("teamPosition") or me.get("individualPosition") or ""),
            team_id=int(me.get("teamId") or 0),
            source="sgp",
        ))
    if not records:
        return MatchHistoryResult(state=NO_DATA, total=0)
    return MatchHistoryResult(state=AVAILABLE, records=records, total=len(records))


# 腾讯服 SGP base：按 platformId 子区映射（参考 Ninohana/lol 公开实现）
_TENCENT_SGP = {
    "HN10": "https://hn10-k8s-sgp.lol.qq.com:21019",
    "CQ100": "https://cq100-sgp.lol.qq.com:21019",
}


class SgpMatchHistoryProvider:
    """SGP 战绩查询。token / base 通过注入的 callable 获得，便于测试与替换。"""

    def __init__(
        self,
        entitlements_token: Callable[[], Optional[str]],
        sgp_base: Callable[[], Optional[str]],
        https_get: Optional[Callable[[str, dict], dict]] = None,
    ):
        self._token = entitlements_token
        self._base = sgp_base
        self._https = https_get or self._default_https

    @staticmethod
    def _default_https(url: str, headers: dict) -> dict:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
            return json.load(r)

    def recent(self, puuid: str, start_index: int = 0, count: int = 20) -> MatchHistoryResult:
        token = self._token()
        base = self._base()
        if not token:
            return MatchHistoryResult(state=AUTH_REQUIRED, message="no_entitlements_token")
        if not base:
            return MatchHistoryResult(state=SGP_UNAVAILABLE, message="no_sgp_base")
        url = f"{base}/match-history-query/v1/products/lol/player/{puuid}/SUMMARY"
        url += f"?startIndex={start_index}&count={count}"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-akari-token-type": "entitlements",
            "Accept": "application/json",
        }
        try:
            payload = self._https(url, headers)
        except Exception as e:
            return MatchHistoryResult(state=SGP_UNAVAILABLE, message=f"request_failed: {e}")
        return parse_sgp_summary(payload, puuid)


def tencent_sgp_base_from_platform(platform_id: str) -> Optional[str]:
    if not platform_id:
        return None
    return _TENCENT_SGP.get(platform_id.upper())
