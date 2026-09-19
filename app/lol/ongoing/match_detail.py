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


@dataclass
class MatchTimelineEvent:
    timestamp_ms: int
    event_type: str
    participant_id: Optional[int] = None
    item_id: Optional[int] = None
    building_type: Optional[str] = None
    monster_type: Optional[str] = None
    killer_id: Optional[int] = None
    victim_id: Optional[int] = None
    assist_ids: list = field(default_factory=list)


def _fmt_ms(ms: int) -> str:
    total = int(ms or 0) // 1000
    m, sec = total // 60, total % 60
    return f"{m:02d}:{sec:02d}"


def parse_details_events(details_json: dict, participant_map: dict = None) -> list:
    events = []
    for frame in (details_json or {}).get("frames", []) or []:
        ts = frame.get("timestamp", 0)
        for ev in frame.get("events", []) or []:
            et = ev.get("type")
            if not et:
                continue
            events.append(MatchTimelineEvent(
                timestamp_ms=ts,
                event_type=et,
                participant_id=ev.get("participantId"),
                item_id=ev.get("itemId") if et in ("ITEM_PURCHASED","ITEM_SOLD","ITEM_DESTROYED") else None,
                building_type=ev.get("buildingType") if et == "BUILDING_KILL" else None,
                monster_type=ev.get("monsterType") if et == "ELITE_MONSTER_KILL" else None,
                killer_id=ev.get("killerId") if et in ("CHAMPION_KILL","BUILDING_KILL","ELITE_MONSTER_KILL","WARD_KILL") else None,
                victim_id=ev.get("victimId") if et == "CHAMPION_KILL" else None,
                assist_ids=ev.get("assistingParticipantIds") or [] if et in ("CHAMPION_KILL","BUILDING_KILL","ELITE_MONSTER_KILL") else [],
            ))
    return events


class MatchTimelineService:
    def __init__(self, lcu_get):
        self._lcu = lcu_get
        self._cache = None

    def _token(self):
        r = self._lcu("/entitlements/v1/token")
        return r.get("accessToken") if isinstance(r, dict) else None

    def _fetch(self, game_id, sub_id=None):
        if self._cache is not None:
            return self._cache
        token = self._token()
        from .sgp_server import SgpServerResolver
        state, base, sid = SgpServerResolver(self._lcu).resolve()
        sub_id = sub_id or sid
        if not base or not sub_id:
            raise RuntimeError(f"sgp server unresolved: state={state}")
        url = f"{base}/match-history-query/v1/products/lol/{sub_id}_{game_id}/DETAILS"
        import json, ssl, urllib.request
        ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {token}",
            "x-akari-token-type": "entitlements",
        })
        raw = json.load(urllib.request.urlopen(req, context=ctx, timeout=20))
        j = raw.get("json", raw)
        pmap = {}
        for pp in j.get("participants", []) or []:
            pid = pp.get("participantId")
            if pid:
                pmap[pid] = pp.get("puuid")
        events = parse_details_events(j, pmap)
        series = {}
        for fr in j.get("frames", []) or []:
            ts = fr.get("timestamp", 0)
            for sid2, cf in (fr.get("participantFrames") or {}).items():
                pid = int(sid2)
                series.setdefault(pid, []).append({
                    "ts": ts,
                    "current_gold": cf.get("currentGold"),
                    "total_gold": cf.get("totalGold"),
                    "level": cf.get("level"),
                    "xp": cf.get("xp"),
                    "minions": cf.get("minionsKilled"),
                    "jungle": cf.get("jungleMinionsKilled"),
                })
        self._cache = {"pmap": pmap, "events": events, "series": series}
        return self._cache

    def get_events(self, game_id, sub_id=None):
        d = self._fetch(game_id, sub_id)
        return d["events"], d["pmap"]

    def get_stats(self, game_id, sub_id=None):
        d = self._fetch(game_id, sub_id)
        return d["pmap"], d["series"]
