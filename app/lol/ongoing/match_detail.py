"""单场详情：SGP 单场 SUMMARY（{subId}_{gameId}/SUMMARY）返回完整 10 人数据。

LeagueAkari: MatchHistoryQueryHttpApi.getGameSummaryByGameId(gameId)
  -> GET {base}/match-history-query/v1/products/lol/{subId}_{gameId}/SUMMARY
  -> { metadata, json: { gameId, participants:[扁平10人 stats] } }
DETAILS 主要是时间线 frames，10 人核心统计在 SUMMARY 单场里。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .match_model import AVAILABLE, NO_DATA, ERROR

AVAILABLE = AVAILABLE


@dataclass
class DetailPlayer:
    puuid: str
    champion_name: str
    team_id: int
    position: str
    win: Optional[bool]
    kills: Optional[int]
    deaths: Optional[int]
    assists: Optional[int]
    cs: Optional[int]
    gold: Optional[int]
    items: List[int]
    spell1: Optional[int]
    spell2: Optional[int]


@dataclass
class MatchDetail:
    game_id: int
    players: List[DetailPlayer]


class MatchDetailService:
    def __init__(self, lcu_get, recent_n=20):
        self._lcu = lcu_get

    def _token(self):
        r = self._lcu("/entitlements/v1/token")
        return r.get("accessToken") if isinstance(r, dict) else None

    def get(self, game_id: int, self_puuid: str, sub_id: str = "HN10"):
        token = self._token()
        base = "https://hn10-k8s-sgp.lol.qq.com:21019"
        url = f"{base}/match-history-query/v1/products/lol/{sub_id}_{game_id}/SUMMARY"
        import json, ssl, urllib.request
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "x-akari-token-type": "entitlements",
        })
        raw = json.load(urllib.request.urlopen(req, context=ctx, timeout=20))
        j = raw.get("json", raw)
        players = []
        for p in j.get("participants", []):
            cs = (p.get("totalMinionsKilled") or 0) + (p.get("neutralMinionsKilled") or 0)
            players.append(DetailPlayer(
                puuid=p.get("puuid", ""),
                champion_name=p.get("championName", ""),
                team_id=int(p.get("teamId") or 0),
                position=p.get("teamPosition") or "",
                win=bool(p.get("win")) if "win" in p else None,
                kills=p.get("kills"), deaths=p.get("deaths"), assists=p.get("assists"),
                cs=cs, gold=p.get("goldEarned"),
                items=[p.get(f"item{i}", 0) or 0 for i in range(7)],
                spell1=p.get("spell1Id"), spell2=p.get("spell2Id"),
            ))
        return MatchDetail(game_id=int(j.get("gameId") or game_id), players=players)
