"""当前对局确定性分析（CurrentGameAnalysis）。

纯函数、可单测，不发网络、不猜数据、不用 LLM 算数字。
"谁最肥" 的确定性规则：
  排序键 = (gold 降序, level 降序, cs 降序)
gold 是 2999 playerlist 提供的可靠字段；缺失则依次回退 level、cs。
"""
from __future__ import annotations

from typing import List, Dict, Any, Optional

from ..lol.snapshot import GameSnapshot, PlayerInfo


def _team_of_self(snapshot: GameSnapshot) -> str:
    cp = snapshot.current_player
    return (cp.team if cp else "") or ""


def _players_in_side(snapshot: GameSnapshot, side: str) -> List[PlayerInfo]:
    """side: 'self' / 'enemy'。按 team 过滤。"""
    self_team = _team_of_self(snapshot)
    out = []
    for p in (snapshot.players or []):
        if not p.champion_name:
            continue
        if side == "self":
            if p.team and p.team == self_team:
                out.append(p)
        else:
            if p.team and p.team != self_team:
                out.append(p)
    return out


def _score(p: PlayerInfo) -> tuple:
    # 越大越靠前；gold 为主
    return (int(p.gold), int(p.level), int(p.cs))


def richest(snapshot: GameSnapshot, side: str = "enemy") -> Optional[Dict[str, Any]]:
    """返回该方最肥玩家（gold/level/cs 排序第一）。无数据返回 None。"""
    players = _players_in_side(snapshot, side)
    if not players:
        return None
    top = max(players, key=_score)
    return {
        "champion": top.champion_name,
        "summoner_name": top.summoner_name,
        "gold": int(top.gold),
        "level": top.level,
        "cs": top.cs,
        "team": top.team,
    }


def self_kda(snapshot: GameSnapshot) -> Optional[Dict[str, Any]]:
    """当前对局自己的 KDA。"""
    cp = snapshot.current_player
    if cp is None:
        return None
    return {
        "champion": cp.champion_name,
        "kills": cp.kills,
        "deaths": cp.deaths,
        "assists": cp.assists,
        "level": cp.level,
        "cs": cp.cs,
    }


def team_rankings(snapshot: GameSnapshot, side: str = "self") -> List[Dict[str, Any]]:
    """该方按发育排序的完整列表。"""
    players = _players_in_side(snapshot, side)
    ordered = sorted(players, key=_score, reverse=True)
    return [
        {"champion": p.champion_name, "gold": int(p.gold),
         "level": p.level, "cs": p.cs, "team": p.team}
        for p in ordered
    ]
