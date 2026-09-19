"""账号近期战绩服务：SGP primary，LCU fallback（腾讯服）。

调用链：
  LCU entitlements token + current puuid
    -> SgpMatchHistoryProvider SUMMARY(count=20)
    -> MatchRecord[20] (gameCreation DESC)
    -> MatchHistoryResult(AVAILABLE/NO_DATA/ERROR)
  SGP 失败 -> 复用现有 LCU v1 match history 作为 fallback。
"""
from __future__ import annotations

import json
from typing import Callable, List, Optional

from .match_model import (
    MatchRecord, MatchHistoryResult,
    AVAILABLE, NO_DATA, ERROR,
)
from .sgp_provider import SgpMatchHistoryProvider, tencent_sgp_base_from_platform

# 最小 queueId -> 模式标签
_QUEUE_LABELS = {
    3140: "新手/训练",
    870: "人机",
    420: "单双排位",
    430: "匹配",
    440: "灵活排位",
    450: "大乱斗",
}


def queue_label(queue_id: int) -> str:
    return _QUEUE_LABELS.get(int(queue_id), "未知模式")


class AccountMatchHistoryService:
    def __init__(
        self,
        lcu_get: Callable[[str], Optional[dict]],
        recent_n: int = 20,
    ):
        self._lcu_get = lcu_get
        self._n = recent_n

    def _entitlements_token(self) -> Optional[str]:
        r = self._lcu_get("/entitlements/v1/token")
        if isinstance(r, dict):
            t = r.get("accessToken")
            if isinstance(t, str) and t:
                return t
        return None

    def _current_puuid(self) -> Optional[str]:
        s = self._lcu_get("/lol-summoner/v1/current-summoner")
        if isinstance(s, dict):
            return s.get("puuid")
        return None

    def recent_matches(self) -> MatchHistoryResult:
        puuid = self._current_puuid()
        if not puuid:
            return MatchHistoryResult(state=ERROR, message="no_self_puuid")

        from .sgp_server import SgpServerResolver
        state, base, sub_id = SgpServerResolver(self._lcu_get).resolve()
        token = self._entitlements_token()
        if token and base:
            prov = SgpMatchHistoryProvider(lambda: token, lambda: base)
            r = prov.recent(puuid, start_index=0, count=self._n)
            if r.ok or r.state == NO_DATA:
                return self._finalize(r)

        # fallback: LCU v1
        return self._lcu_fallback(puuid)

    def _lcu_fallback(self, puuid: str) -> MatchHistoryResult:
        data = self._lcu_get(
            f"/lol-match-history/v1/products/lol/{puuid}/games?begIndex=0&endIndex={self._n}")
        if not isinstance(data, dict):
            return MatchHistoryResult(state=ERROR, message="lcu_unavailable")
        out: List[MatchRecord] = []
        for g in (data.get("games", {}).get("games") or []):
            raw = g.get("json")
            j = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
            me = next((p for p in j.get("participants", []) if p.get("puuid") == puuid), None)
            if not me:
                continue
            out.append(MatchRecord(
                game_id=int(j.get("gameId") or 0),
                game_creation=int(j.get("gameCreation") or 0),
                game_duration=int(j.get("gameDuration") or 0),
                queue_id=int(j.get("queueId") or 0),
                champion_name=str(me.get("championName") or ""),
                win=bool(me.get("win")) if "win" in me else None,
                kills=me.get("kills"), deaths=me.get("deaths"), assists=me.get("assists"),
                source="lcu",
            ))
        if not out:
            return MatchHistoryResult(state=NO_DATA)
        return self._finalize(MatchHistoryResult(state=AVAILABLE, records=out))

    def self_puuid(self):
        return self._current_puuid()

    def get_detail(self, game_id: int, self_puuid: str, sub_id: str = None):
        from .match_detail import MatchDetailService
        from .sgp_server import SgpServerResolver
        if sub_id is None:
            _, base, sub_id = SgpServerResolver(self._lcu_get).resolve()
        return MatchDetailService(self._lcu_get).get(game_id, self_puuid, sub_id or "HN10")

    @staticmethod
    def _finalize(r: MatchHistoryResult) -> MatchHistoryResult:
        r.records.sort(key=lambda m: m.game_creation, reverse=True)
        for m in r.records:
            m.queue_type = queue_label(m.queue_id)
        return r
