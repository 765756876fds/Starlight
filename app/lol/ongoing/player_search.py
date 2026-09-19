"""A9.6 玩家战绩搜索：Riot ID -> puuid -> SGP 近期战绩。

只支持当前所在腾讯大区（与登录账号同服务器）。
Riot ID 格式：name#tag，如 果子#48230。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional, List
import json

from .match_model import MatchRecord, MatchHistoryResult, AVAILABLE, NO_DATA, ERROR
from .sgp_server import SgpServerResolver

FOUND = "FOUND"
NOT_FOUND = "NOT_FOUND"
INVALID_RIOT_ID = "INVALID_RIOT_ID"
SEARCH_UNAVAILABLE = "SEARCH_UNAVAILABLE"
SERVICE_ERROR = "ERROR"


@dataclass
class PlayerSearchResult:
    status: str
    puuid: Optional[str] = None
    game_name: str = ""
    tag_line: str = ""
    matches: Optional[List[MatchRecord]] = None
    message: str = ""


def parse_riot_id(text: str):
    s = (text or "").strip().replace(" ", "")
    if "#" not in s:
        return None, None
    parts = s.split("#")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None, None
    return parts[0], parts[1]


class PlayerSearchService:
    def __init__(self, lcu_get: Callable, lcu_post: Callable, recent_n: int = 20):
        self._lcu = lcu_get
        self._post = lcu_post
        self._n = recent_n

    def _token(self):
        r = self._lcu("/entitlements/v1/token")
        return r.get("accessToken") if isinstance(r, dict) else None

    def search(self, riot_id: str) -> PlayerSearchResult:
        gn, tag = parse_riot_id(riot_id)
        if not gn or not tag:
            return PlayerSearchResult(status=INVALID_RIOT_ID, message="请输入 名称#标签，例如 名字#48230")

        # LeagueAkari 已确认的真实 Riot ID -> puuid 接口：
        # POST /lol-summoner/v1/summoners/aliases  body=[{gameName, tagLine}]
        # lcu_post 在 2xx 返回 body(list)，否则返回 None。
        try:
            rows = self._post(
                "/lol-summoner/v1/summoners/aliases",
                [{"gameName": gn, "tagLine": tag}],
            )
        except Exception:
            return PlayerSearchResult(status=SEARCH_UNAVAILABLE, message="搜索服务不可用")

        if rows is None:
            # LCU HTTP / 认证 / 连接异常，不是“玩家不存在”
            return PlayerSearchResult(status=SEARCH_UNAVAILABLE, message="搜索服务不可用")

        if not isinstance(rows, list):
            return PlayerSearchResult(status=SEARCH_UNAVAILABLE, message="搜索服务返回异常")

        if not rows:
            return PlayerSearchResult(status=NOT_FOUND, message="未找到玩家")

        target = rows[0] if isinstance(rows[0], dict) else None
        if not target or not target.get("puuid"):
            return PlayerSearchResult(status=SEARCH_UNAVAILABLE, message="搜索结果异常，缺少 PUUID")

        puuid = target["puuid"]
        # SGP 战绩
        from .sgp_provider import SgpMatchHistoryProvider
        _, base, sub = SgpServerResolver(self._lcu).resolve()
        token = self._token()
        if not (base and token):
            return PlayerSearchResult(status=SEARCH_UNAVAILABLE, message="战绩源不可用")
        prov = SgpMatchHistoryProvider(lambda: token, lambda: base)
        res = prov.recent(puuid, start_index=0, count=self._n)
        recs = sorted(res.records, key=lambda m: m.game_creation, reverse=True)
        return PlayerSearchResult(
            status=FOUND if recs else NO_DATA,
            puuid=puuid,
            game_name=target.get("gameName") or gn,
            tag_line=target.get("tagLine") or tag,
            matches=recs,
        )
