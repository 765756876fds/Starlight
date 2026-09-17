"""PlayerQuery handler - CommandBus handler with self identity + speech output"""
import time

from ..core.command_bus import Command
from ..speech.speech_queue import SpeechItem, PRIORITY_B
from .query_service import PlayerQueryService
from .response_formatter import format_query_result


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


def make_player_query_handler(service: PlayerQueryService,
                              speech_queue=None,
                              state_store=None):
    def handle(command: Command):
        params = command.params or {}
        query_type = params.get("query_type", "recent")
        identity = params.get("identity")
        limit = params.get("limit")
        local_match_id = params.get("local_match_id")

        # 显式 identity 优先；否则尝试 self
        if not identity:
            self_puuid = _resolve_self_puuid(state_store)
            if self_puuid:
                identity = {"puuid": self_puuid}

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
