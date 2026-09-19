"""单场详情：SGP 单场 SUMMARY（{subId}_{gameId}/SUMMARY）返回完整 10 人数据。

LeagueAkari: MatchHistoryQueryHttpApi.getGameSummaryByGameId(gameId)
  -> GET {base}/match-history-query/v1/products/lol/{subId}_{gameId}/SUMMARY
  -> { metadata, json: { gameId, participants:[扁平10人 stats] } }
DETAILS 主要是时间线 frames，10 人核心统计在 SUMMARY 单场里。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .match_model import AVAILABLE, NO_DATA, ERROR

AVAILABLE = AVAILABLE


@dataclass
class DetailPlayer:
    puuid: str
    name: str
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
    level: Optional[int] = None
    double_kills: Optional[int] = None
    triple_kills: Optional[int] = None
    quadra_kills: Optional[int] = None
    penta_kills: Optional[int] = None
    vision_score: Optional[int] = None
    wards_placed: Optional[int] = None
    wards_killed: Optional[int] = None
    dmg_to_champ: Optional[int] = None
    dmg_taken: Optional[int] = None
    magic_dmg: Optional[int] = None
    phys_dmg: Optional[int] = None
    true_dmg: Optional[int] = None
    # 符文：(主系 tree id, 副系 tree id, [主系 perk ids], [副系 perk ids], {小符文:id})
    primary_style: Optional[int] = None
    secondary_style: Optional[int] = None
    primary_perks: List[int] = field(default_factory=list)
    secondary_perks: List[int] = field(default_factory=list)
    stat_perks: List[int] = field(default_factory=list)
    privacy: str = ""  # PUBLIC / PRIVATE / 空（未知）


@dataclass
class MatchDetail:
    game_id: int
    players: List[DetailPlayer]


def _parse_perks(perks: dict):
    if not isinstance(perks, dict):
        return None, None, [], [], []
    styles = perks.get("styles") or []
    primary_style = secondary_style = None
    primary_perks, secondary_perks = [], []
    ordered = [s for s in styles if isinstance(s, dict)]
    if len(ordered) >= 1:
        primary_style = ordered[0].get("style")
        primary_perks = [x.get("perk") for x in (ordered[0].get("selections") or []) if isinstance(x, dict)]
    if len(ordered) >= 2:
        secondary_style = ordered[1].get("style")
        secondary_perks = [x.get("perk") for x in (ordered[1].get("selections") or []) if isinstance(x, dict)]
    sp = perks.get("statPerks") or {}
    stat = [sp.get("offense"), sp.get("flex"), sp.get("defense")]
    return primary_style, secondary_style, primary_perks, secondary_perks, stat


class MatchDetailService:
    def __init__(self, lcu_get, recent_n=20):
        self._lcu = lcu_get

    def _token(self):
        r = self._lcu("/entitlements/v1/token")
        return r.get("accessToken") if isinstance(r, dict) else None

    def get(self, game_id: int, self_puuid: str, sub_id: str = None):
        token = self._token()
        from .sgp_server import SgpServerResolver
        state, base, sid = SgpServerResolver(self._lcu).resolve()
        sub_id = sub_id or sid
        if not base or not sub_id:
            raise RuntimeError(f"sgp server unresolved: state={state}")
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
            ps, ss, pp, sp, statp = _parse_perks(p.get("perks"))
            players.append(DetailPlayer(
                puuid=p.get("puuid", ""),
                name=p.get("gameName") or p.get("summonerName") or p.get("riotIdGameName") or "",
                champion_name=p.get("championName", ""),
                team_id=int(p.get("teamId") or 0),
                position=p.get("teamPosition") or "",
                win=bool(p.get("win")) if "win" in p else None,
                kills=p.get("kills"), deaths=p.get("deaths"), assists=p.get("assists"),
                cs=cs, gold=p.get("goldEarned"),
                items=[p.get(f"item{i}", 0) or 0 for i in range(7)],
                spell1=p.get("spell1Id"), spell2=p.get("spell2Id"),
                level=p.get("champLevel"),
                double_kills=p.get("doubleKills"), triple_kills=p.get("tripleKills"),
                quadra_kills=p.get("quadraKills"), penta_kills=p.get("pentaKills"),
                vision_score=p.get("visionScore"),
                wards_placed=p.get("wardsPlaced"), wards_killed=p.get("wardsKilled"),
                dmg_to_champ=p.get("totalDamageDealtToChampions"),
                dmg_taken=p.get("totalDamageTaken"),
                magic_dmg=p.get("magicDamageDealtToChampions"),
                phys_dmg=p.get("physicalDamageDealtToChampions"),
                true_dmg=p.get("trueDamageDealtToChampions"),
                primary_style=ps, secondary_style=ss,
                primary_perks=pp, secondary_perks=sp, stat_perks=statp,
            ))
        # A10.2：一次批量查询 10 人 privacy，失败不影响基础详情
        try:
            from .summoner_privacy import SummonerPrivacyService
            batch = SummonerPrivacyService(self._lcu).batch_privacy([pl.puuid for pl in players])
            for pl in players:
                pl.privacy = batch.get(pl.puuid, "")
        except Exception:
            pass
        return MatchDetail(game_id=int(j.get("gameId") or game_id), players=players)
