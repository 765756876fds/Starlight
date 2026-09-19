"""PlayerQuery handler - CommandBus handler with self identity + speech output"""
import time

from ..core.command_bus import Command
from ..speech.speech_queue import SpeechItem, PRIORITY_B
from .query_service import PlayerQueryService
from .response_formatter import format_query_result
from .current_game_analysis import richest as _richest, self_kda as _self_kda


def _resolve_self_puuid(state_store) -> str:
    """从 StateStore 读 current_summoner.puuid，不直接调 LCU。"""
    if state_store is None:
        return None
    try:
        summ = state_store.get("lcu", "current_summoner")
        if summ and isinstance(summ, dict):
            return summ.get("puuid") or None
    except Exception:
        pass
    return None


def _normalize_identity(identity, state_store):
    """归一化 LLM / Rule 传来的 identity 字段，保证进入 QueryService 的是 dict 或 None。

    兼容：
      {"puuid": "..."}        → 原样
      {"riot_id": "..."}      → 原样
      "self"                  → {"self": True}
      {"self": True}          → {"self": True}
      None / 123 / [] / {}    → None
    非法类型不猜测、不崩溃。
    """
    if identity is None:
        return None
    if isinstance(identity, str):
        if identity.strip().lower() == "self":
            return {"self": True}
        # 其他字符串（如 "xxx#1234"）不自动猜为 riot_id
        return None
    if not isinstance(identity, dict):
        return None
    if not identity:
        return None
    return identity


def make_player_query_handler(service: PlayerQueryService,
                              speech_queue=None,
                              state_store=None,
                              snapshot_provider=None):
    def handle(command: Command):
        params = command.params or {}
        query_type = params.get("query_type", "recent")
        identity = params.get("identity")
        limit = params.get("limit")
        local_match_id = params.get("local_match_id")

        # 归一化 identity
        identity = _normalize_identity(identity, state_store)

        # self 归一化后走 StateStore → current_summoner.puuid
        if identity and identity.get("self"):
            self_puuid = _resolve_self_puuid(state_store)
            if self_puuid:
                identity = {"puuid": self_puuid}
            else:
                identity = None

        # 无 identity → 也尝试 self（兼容 RuleBasedIntent 无 identity 的情况）
        if not identity:
            self_puuid = _resolve_self_puuid(state_store)
            if self_puuid:
                identity = {"puuid": self_puuid}

        qt = params.get("query_type", "recent")
        # --- 当前对局实时分析（纯确定性，读 live snapshot）---
        if qt in ("current_richest", "current_self_kda"):
            snap = snapshot_provider() if snapshot_provider else None
            text = "现在读不到对局数据。"
            if snap is not None:
                if qt == "current_self_kda":
                    k = _self_kda(snap)
                    text = (f"你现在是 {k['champion']}，{k['kills']} 杀 {k['deaths']} 死 "
                            f"{k['assists']} 助攻，{k['cs']} 刀。") if k else text
                else:
                    side = params.get("side", "enemy")
                    r = _richest(snap, side)
                    if r:
                        who = "对面" if side == "enemy" else "我们"
                        text = f"{who}最肥的是 {r['champion']}，{r['level']} 级 {r['gold']} 块。"
            if speech_queue is not None and text:
                try:
                    speech_queue.enqueue(SpeechItem(
                        text=text, priority=PRIORITY_B,
                        expire_at=time.time() + 30.0,
                        category="player_query", interruptible=True))
                except Exception:
                    pass
            return {"query_type": qt, "result": text, "data_quality": {"status": "OK"}}

        result = service.query(
            query_type=query_type,
            identity=identity,
            limit=limit,
            local_match_id=local_match_id,
        )

        # 格式化 + 播报
        text = format_query_result(result)
        if speech_queue is not None and text:
            try:
                speech_queue.enqueue(SpeechItem(
                    text=text,
                    priority=PRIORITY_B,
                    expire_at=time.time() + 30.0,
                    category="player_query",
                    interruptible=True,
                ))
            except Exception:
                pass

        return result
    return handle
