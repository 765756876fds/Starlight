"""A9.6-R5 查询召唤师隐私状态（LeagueAkari 真实链路最小迁移）。

LeagueAkari 源码确认：
  GET /lol-league-session/v1/league-session-token  -> bare JSON string (league-session token)
  POST {SGP_BASE}/summoner-ledge/v1/regions/{SUB_ID}/summoners/puuids
      body: ["<puuid>"]
      headers:
        Authorization: Bearer <league-session-token>
        x-akari-sgp-server-id: <SUB_ID>
        x-akari-token-type: league-session
        x-akari-force-stream-collect: true
  -> SgpSummonerLol[] ，取 [0].privacy == "PRIVATE" -> 隐藏战绩

错误语义严格区分：
  PUBLIC / PRIVATE / NOT_FOUND / ERROR
"""
from __future__ import annotations

import json
import ssl
import urllib.request
from typing import Callable, Optional

from .sgp_server import SgpServerResolver

PUBLIC = "PUBLIC"
PRIVATE = "PRIVATE"
NOT_FOUND = "NOT_FOUND"
ERROR = "ERROR"


class SummonerPrivacyService:
    """查询单个 puuid 的战绩可见性（privacy）。"""

    def __init__(self, lcu_get: Callable, https_post=None):
        self._lcu_get = lcu_get
        self._https_post = https_post or self._default_post

    def _league_session_token(self) -> Optional[str]:
        # 返回裸 JSON 字符串
        r = self._lcu_get("/lol-league-session/v1/league-session-token")
        if isinstance(r, str) and r:
            return r
        if isinstance(r, dict):
            return r.get("token") or r.get("accessToken")
        return None

    @staticmethod
    def _default_post(url: str, body, headers: dict) -> dict:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.load(resp)

    def get_privacy(self, puuid: str) -> str:
        if not puuid:
            return ERROR
        token = self._league_session_token()
        _, base, sub = SgpServerResolver(self._lcu_get).resolve()
        if not (token and base and sub):
            return ERROR
        url = f"{base}/summoner-ledge/v1/regions/{sub}/summoners/puuids"
        headers = {
            "Authorization": f"Bearer {token}",
            "x-akari-sgp-server-id": sub,
            "x-akari-token-type": "league-session",
            "x-akari-force-stream-collect": "true",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            data = self._https_post(url, [puuid], headers)
        except Exception:
            return ERROR
        if not isinstance(data, list):
            return ERROR
        if not data:
            return NOT_FOUND
        first = data[0]
        if not isinstance(first, dict):
            return ERROR
        priv = first.get("privacy")
        if priv == "PRIVATE":
            return PRIVATE
        if priv == "PUBLIC":
            return PUBLIC
        return ERROR
