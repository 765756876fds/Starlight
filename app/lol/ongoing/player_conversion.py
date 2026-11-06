"""Gameflow/选人玩家字段到当前对局玩家模型的纯转换。"""
from __future__ import annotations

from .session import CurrentGamePlayer


def to_current_player(cell: dict, team: str, me_puuid: str | None) -> CurrentGamePlayer:
    """gameflow/champ-select cell → CurrentGamePlayer（identity + realtime 两区）。

    identity 唯一来源：gameflow teamOne/teamTwo（或同局 champ-select 缓存回退）；
    realtime 唯一来源：_merge_live_fields 合并后的 2999 字段（cell.live_*）。
    history 区由 _load_in_thread 加载完成后挂载。
    """
    puuid = cell.get("puuid") or ""
    return CurrentGamePlayer(
        puuid=puuid,
        summoner_id=cell.get("summonerId") or 0,
        display_name=(cell.get("riotIdGameName") or cell.get("riotId")
                      or cell.get("name") or cell.get("summonerName") or ""),
        cell_id=cell.get("cellId") or 0,
        team=team,
        assigned_position=(cell.get("assignedPosition") or "") or None,
        champion_id=cell.get("championId") or 0,
        champion_pick_intent=cell.get("championPickIntent") or 0,
        is_self=(puuid == me_puuid),
        live_kills=cell.get("live_kills") or 0,
        live_deaths=cell.get("live_deaths") or 0,
        live_assists=cell.get("live_assists") or 0,
        live_position=(cell.get("live_position") or "") or None,
        live_level=cell.get("live_level") or 0,
        realtime_available=bool(cell.get("live_merged")),
    )
