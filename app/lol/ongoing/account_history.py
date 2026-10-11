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
    0: "自定义",
    3140: "新手/训练",
    870: "人机",
    890: "极限闪击",
    840: "人机",
    830: "人机",
    850: "人机",
    400: "匹配",
    430: "匹配",
    460: "匹配",
    490: "匹配",
    900: "无限火力",
    1010: "无限火力",
    450: "大乱斗",
    900: "无限火力",
    1900: "无限火力",
    420: "单双排位",
    430: "匹配",
    440: "灵活排位",
    470: "双人排位",
    700: "克隆",
    720: "围场",
    800: "人机",
    820: "人机",
    900: "无限火力",
    1020: "ONE for ALL",
    1300: "纳什男爵",
}


def queue_label(queue_id: int) -> str:
    q = int(queue_id or 0)
    return _QUEUE_LABELS.get(q, f'未知模式({q})')


class AccountMatchHistoryService:

    def __init__(self, lcu_get, lcu_post=None, recent_n=20, lcu_conn=None):
        self._lcu_get = lcu_get
        self._lcu_post = lcu_post or (lambda ep, b: None)
        self._n = recent_n
        # 独立 LCU session：绕过主进程 LCUConnection 的共享锁/断连窗口。
        # 主进程任一请求异常会触发 _disconnect()，5秒重连窗口内 lcu.get 全部返回 None，
        # 导致 SGP 前置(resolver/token)偶发失败 -> fallback lcu_unavailable。
        # 惰性直连：_direct_get 每次实时取 lcu_conn 当前 base/token，
        # 避免构造时序早于 lcu discovery（_port/_auth_token 未就绪）导致直连永久失效。
        self._lcu_conn = lcu_conn
        self._direct = None

    def _direct_get(self, ep: str):
        """SGP 前置专用 getter：优先独立 session（惰性取 base/token），退化到共享 lcu_get。"""
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
        return self._lcu_get(ep)

    def _entitlements_token(self):
        r = self._direct_get("/entitlements/v1/token")
        if isinstance(r, dict):
            t = r.get("accessToken")
            if isinstance(t, str) and t:
                return t
        return None

    def _current_puuid(self) -> Optional[str]:
        s = self._direct_get("/lol-summoner/v1/current-summoner")
        if isinstance(s, dict):
            return s.get("puuid")
        return None

    def recent_matches(self) -> MatchHistoryResult:
        puuid = self._current_puuid()
        if not puuid:
            return MatchHistoryResult(state=ERROR, message="no_self_puuid")
        return self.recent(puuid)

    def recent(self, puuid: str, count: Optional[int] = None) -> MatchHistoryResult:
        """按 puuid 查任意玩家最近战绩（SGP primary，LCU fallback）。

        房间 10 人批量查询统一入口。以 puuid 为主键，不依赖昵称。
        """
        if not puuid:
            return MatchHistoryResult(state=ERROR, message="no_puuid")
        n = self._n if count is None else int(count)

        from .sgp_server import SgpServerResolver
        state, base, sub_id = SgpServerResolver(self._direct_get).resolve()
        token = self._entitlements_token()
        if not base:
            # resolver 偶发失败：立即重试一次
            state, base, sub_id = SgpServerResolver(self._direct_get).resolve()
        if token and base:
            prov = SgpMatchHistoryProvider(lambda: token, lambda: base)
            r = prov.recent(puuid, start_index=0, count=n)
            if r.ok or r.state == NO_DATA:
                return self._finalize(r)
            # SGP 请求偶发失败（进游戏初期常见）：立即重试一次再放弃
            if r.state in ("SGP_UNAVAILABLE", "AUTH_REQUIRED", "ERROR"):
                r = prov.recent(puuid, start_index=0, count=n)
                if r.ok or r.state == NO_DATA:
                    return self._finalize(r)

        # fallback: LCU v1（国服通常不可用，仅兜底）
        return self._lcu_fallback(puuid)

    def _lcu_fallback(self, puuid: str) -> MatchHistoryResult:
        data = self._lcu_get(
            f"/lol-match-history/v1/products/lol/{puuid}/games?begIndex=0&endIndex={self._n}")
        if not isinstance(data, dict):
            return MatchHistoryResult(state=ERROR, message="lcu_unavailable")
        out: List[MatchRecord] = []
        for g in (data.get("games", {}).get("games") or []):
            raw = g.get("json")
            try:
                j = json.loads(raw) if isinstance(raw, str) else (raw if isinstance(raw, dict) else {})
            except Exception:
                # 单条坏数据（如验证码 HTML）跳过，不中断整个 fallback
                continue
            me = next((p for p in j.get("participants", []) if p.get("puuid") == puuid), None)
            if not me:
                continue
            # 兼容两种 participants 字段格式：顶层 win/kills/deaths/assists（LCU 官方），
            # 或 stats 嵌套（历史 fixture / 部分腾讯服返回）
            stats = me.get("stats") or {}
            win = me.get("win")
            if win is None and "win" in stats:
                win = stats.get("win")
            kills = me.get("kills") if "kills" in me else stats.get("kills")
            deaths = me.get("deaths") if "deaths" in me else stats.get("deaths")
            assists = me.get("assists") if "assists" in me else stats.get("assists")
            out.append(MatchRecord(
                game_id=int(j.get("gameId") or 0),
                game_creation=int(j.get("gameCreation") or 0),
                game_duration=int(j.get("gameDuration") or 0),
                queue_id=int(j.get("queueId") or 0),
                champion_name=str(me.get("championName") or ""),
                win=bool(win) if win is not None else None,
                kills=kills, deaths=deaths, assists=assists,
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
        return MatchDetailService(self._lcu_get).get(game_id, self_puuid, sub_id)

    @staticmethod
    def _finalize(r: MatchHistoryResult) -> MatchHistoryResult:
        r.records.sort(key=lambda m: m.game_creation, reverse=True)
        for m in r.records:
            m.queue_type = queue_label(m.queue_id)
        return r


