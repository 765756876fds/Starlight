"""SGP GSM 补充身份源（LeagueAkari gsm.ts 最小迁移；实时只读验证已打通）。

作用：Gameflow teamOne/teamTwo 缺失身份时，用 GSM（Game Service Module）按 self puuid
查询当前对局 teamOne/teamTwo，按 puuid 只追加 Gameflow 完全没有的玩家。

调用链（已实测，gameId=9010119520 InProgress）：
  GET {SGP_BASE}/gsm/v1/ledge/region/{sub_id}/puuid/{self_puuid}
      headers: Authorization: Bearer <league-session-token>
  -> 200: { game: { teamOne[], teamTwo[], playerChampionSelections[] },
            playerCredentials: { gameId, ... } }
  -> 404 (RESOURCE_NOT_FOUND): 不在对局/查询不到（Lobby 阶段正常），按 None 降级

约束（实施报告）：
- 只作 Gameflow 的补充：merge key = puuid；Gameflow 已有 puuid 保持原值，绝不覆盖。
- merge 前必须同局校验（GSM playerCredentials.gameId == Gameflow session.gameId），
  任一缺失/不一致一律不 merge，禁止跨局污染。
- 任何失败（token/resolver/HTTP/JSON/结构异常）返回 None，由调用方无损降级。
- league-session token 与 entitlements token 不同，绝不混用；不打印/记录完整 token。
"""
from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

from .sgp_server import SgpServerResolver

# GSM 查询结果标记（供 ongoing.gsm_supplement 事件只读诊断；不携带 token/完整 puuid）
OK = "ok"
NO_TOKEN = "no_token"
RESOLVER_FAILED = "resolver_failed"
HTTP_404 = "http_404"
HTTP_ERROR = "http_error"
REQUEST_FAILED = "request_failed"
BAD_JSON = "bad_json"
NO_GAME = "no_game"


def _default_https(url: str, headers: dict) -> dict:
    """最小 HTTPS GET（与 sgp_provider._default_https 相同；SSL 不校验）。"""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(url, headers=headers)
    # 腾讯 GSM 为国内域名，必须直连，不能走系统代理（否则被导到国外节点）
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler(context=ctx),
    )
    with opener.open(req, timeout=15) as r:
        return json.load(r)


class SgpGsmProvider:
    """GSM 当前对局查询。token / base / HTTP 均注入或默认实现，便于测试替换。"""

    def __init__(
        self,
        lcu_get: Optional[Callable[[str], object]] = None,
        lcu_conn=None,
        https_get: Optional[Callable[[str, dict], dict]] = None,
    ):
        self._lcu_get = lcu_get
        self._lcu_conn = lcu_conn
        self._https = https_get or _default_https

    # ---- LCU 前置（独立直连，绕共享断连窗口；惰性取 base/token）----
    def _direct_get(self, ep: str):
        """SGP/GSM 前置专用 getter：优先独立 session（惰性取 base/token），退化共享 lcu_get。"""
        conn = self._lcu_conn
        if conn is not None:
            base = getattr(conn, "base_url", "") or ""
            token = getattr(conn, "_auth_token", "") or ""
            if base and token:
                try:
                    import requests
                    sess = requests.Session()
                    sess.auth = ("riot", token)
                    sess.verify = False
                    r = sess.get(f"{base}{ep}", timeout=5)
                    return r.json() if r.status_code in (200, 201, 204) else None
                except Exception:
                    return None
        return self._lcu_get(ep) if self._lcu_get is not None else None

    def _league_session_token(self) -> Optional[str]:
        """league-session token：GET /lol-league-session/v1/league-session-token（裸字符串）。

        与 entitlements token 不同，绝不混用。
        """
        r = self._direct_get("/lol-league-session/v1/league-session-token")
        if isinstance(r, str) and r:
            return r
        return None

    # ---- GSM 查询 ----
    def get_current_game(self, self_puuid: str) -> Tuple[str, Optional[dict]]:
        """查询 self 当前对局（GSM）。返回 (结果标记, game payload dict 或 None)。"""
        if not self_puuid:
            return NO_GAME, None
        token = self._league_session_token()
        if not token:
            return NO_TOKEN, None
        state, base, sub_id = SgpServerResolver(self._direct_get).resolve()
        if state != "RESOLVED" or not base or not sub_id:
            return RESOLVER_FAILED, None
        url = f"{base}/gsm/v1/ledge/region/{sub_id}/puuid/{self_puuid}"
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        try:
            payload = self._https(url, headers)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return HTTP_404, None
            return HTTP_ERROR, None
        except Exception:
            return REQUEST_FAILED, None
        if not isinstance(payload, dict):
            return BAD_JSON, None
        if not isinstance(payload.get("game"), dict):
            return NO_GAME, None
        return OK, payload


def _gsm_cell(m: dict) -> dict:
    """GSM member -> gameflow 兼容 cell（字段语义与 _gf_cell 对齐，供 _to_player 消费）。"""
    return {
        "puuid": m.get("puuid") or "",
        "summonerId": m.get("summonerId") or 0,
        "cellId": m.get("cellId") or 0,
        "riotIdGameName": m.get("summonerName") or "",
        "riotId": m.get("riotId") or "",
        "championId": m.get("championId") or 0,
        "assignedPosition": (m.get("selectedPosition") or m.get("selectedRole") or "") or "",
        "gsm_supplemented": True,
    }


def _gsm_game_id(gsm_game: dict) -> Optional[int]:
    """从 GSM payload 取 gameId：优先 playerCredentials.gameId，其次 game.id。"""
    pc = gsm_game.get("playerCredentials")
    if isinstance(pc, dict):
        gid = pc.get("gameId")
        if gid:
            return int(gid)
    g = gsm_game.get("game")
    if isinstance(g, dict):
        gid = g.get("id")
        if gid:
            return int(gid)
    return None


def merge_gsm_players(
    gameflow_session: dict,
    gsm_game: dict,
    me_puuid: Optional[str],
) -> Tuple[dict, int, bool]:
    """按 puuid 追加 GSM 独有玩家到 gameflow session（myTeam/theirTeam）。

    返回 (session, added_count, matched)：
    - matched=False：同局校验失败（GF gameId 缺失 / GSM gameId 缺失 / 不一致）
      或 self 不在 GSM 任一队（数据异常）→ 返回原 session、added=0，禁止跨局/跨侧污染。
    - matched=True：只追加 Gameflow 完全没有的 puuid；已有 puuid 的 cell 原值不动。
    """
    gf_game_id = gameflow_session.get("gameId")
    gsm_id = _gsm_game_id(gsm_game)
    if not gf_game_id or gsm_id is None or int(gf_game_id) != int(gsm_id):
        return gameflow_session, 0, False

    game = gsm_game.get("game") or {}
    gt1 = game.get("teamOne") or []
    gt2 = game.get("teamTwo") or []
    if not isinstance(gt1, list) or not isinstance(gt2, list):
        return gameflow_session, 0, False

    # 判定 GSM my/their 侧（与 Gameflow 同规则：me 在哪一侧，哪侧是 my）
    t1_puuids = {(m.get("puuid") or "") for m in gt1 if m.get("puuid")}
    t2_puuids = {(m.get("puuid") or "") for m in gt2 if m.get("puuid")}
    if me_puuid in t1_puuids:
        gsm_my, gsm_their = gt1, gt2
    elif me_puuid in t2_puuids:
        gsm_my, gsm_their = gt2, gt1
    else:
        return gameflow_session, 0, False

    existing = set()
    for team_key in ("myTeam", "theirTeam"):
        for c in (gameflow_session.get(team_key) or []):
            if c.get("puuid"):
                existing.add(c["puuid"])

    added = 0
    for team_key, gsm_team in (("myTeam", gsm_my), ("theirTeam", gsm_their)):
        cells = gameflow_session.setdefault(team_key, [])
        for m in gsm_team:
            pu = m.get("puuid") or ""
            if not pu or pu in existing:
                continue
            cells.append(_gsm_cell(m))
            existing.add(pu)
            added += 1
    return gameflow_session, added, True
