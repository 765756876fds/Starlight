"""当前对局确定性分析（CurrentGameAnalysis）。

纯函数、可单测，不发网络、不猜数据、不用 LLM 算数字。
"谁最肥" 的确定性规则：
  排序键 = (gold 降序, level 降序, cs 降序)
gold 是 2999 playerlist 提供的可靠字段；缺失则依次回退 level、cs。
"""
from __future__ import annotations

from typing import Any

from ..lol.snapshot import GameSnapshot, PlayerInfo


def _team_of_self(snapshot: GameSnapshot) -> str:
    cp = snapshot.current_player
    return (cp.team if cp else "") or ""


def _players_in_side(snapshot: GameSnapshot, side: str) -> list[PlayerInfo]:
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


def richest(snapshot: GameSnapshot, side: str = "enemy") -> dict[str, Any] | None:
    """返回该方最肥玩家（gold/level/cs 排序第一）。无数据返回 None。

    richness_basis:
      - "gold": 敌方 gold 数据可用，按经济排序
      - "development_proxy": 敌方 gold 不可用，按等级/补刀近似
      - "unavailable": 完全无数据
    """
    players = _players_in_side(snapshot, side)
    if not players:
        return None
    top = max(players, key=_score)

    # 判断 gold 是否可用
    has_gold = any(int(p.gold) > 0 for p in players)
    if has_gold:
        basis = "gold"
    else:
        basis = "development_proxy"

    return {
        "champion": top.champion_name,
        "summoner_name": top.summoner_name,
        "gold": int(top.gold),
        "level": top.level,
        "cs": top.cs,
        "team": top.team,
        "richness_basis": basis,
    }


def self_kda(snapshot: GameSnapshot) -> dict[str, Any] | None:
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


def team_kda(snapshot: GameSnapshot, side: str = "self") -> dict[str, int]:
    players = _players_in_side(snapshot, side)
    return {
        "kills": sum(int(p.kills) for p in players),
        "deaths": sum(int(p.deaths) for p in players),
        "assists": sum(int(p.assists) for p in players),
        "n": len(players),
    }


# 经济差“接近”阈值（金）：|diff| < 该值视为接近。简单固定阈值，代码内写明。
ECONOMY_EVEN_THRESHOLD = 2000


def economy(snapshot: GameSnapshot) -> dict[str, Any]:
    """双方总经济 + 差值 + 领先/落后/接近。纯确定性，不用 LLM/胜率。

    “接近/领先/落后”只基于固定阈值 ECONOMY_EVEN_THRESHOLD。
    team=UNKNOWN 的玩家不计入任何一方（也不在这里统计，由调用方决定）。
    """
    ours = _players_in_side(snapshot, "self")
    enemies = _players_in_side(snapshot, "enemy")
    our_total = sum(int(p.gold) for p in ours)
    enemy_total = sum(int(p.gold) for p in enemies)
    diff = our_total - enemy_total
    if abs(diff) < ECONOMY_EVEN_THRESHOLD:
        state = "even"
    elif diff > 0:
        state = "ahead"
    else:
        state = "behind"
    return {
        "our_total": our_total,
        "enemy_total": enemy_total,
        "diff": diff,
        "abs_diff": abs(diff),
        "state": state,
        "our_n": len(ours),
        "enemy_n": len(enemies),
    }


def team_rankings(snapshot: GameSnapshot, side: str = "self") -> list[dict[str, Any]]:
    """该方按发育排序的完整列表（gold↓→level↓→cs↓）。"""
    players = _players_in_side(snapshot, side)
    ordered = sorted(players, key=_score, reverse=True)
    return [
        {"champion": p.champion_name, "gold": int(p.gold),
         "level": p.level, "cs": p.cs, "team": p.team}
        for p in ordered
    ]


def enemy_roster(snapshot: GameSnapshot) -> list[dict[str, Any]]:
    """返回敌方玩家列表（英雄 + 等级）。"""
    players = _players_in_side(snapshot, "enemy")
    return [
        {"champion": p.champion_name, "level": p.level, "summoner_name": p.summoner_name}
        for p in players
    ]


def most_deaths(snapshot: GameSnapshot) -> dict[str, Any] | None:
    """返回全场死亡最多的玩家。并列时返回所有。"""
    players = [p for p in (snapshot.players or []) if p.champion_name]
    if not players:
        return None
    max_deaths = max(int(p.deaths) for p in players)
    if max_deaths == 0:
        return None
    tied = [p for p in players if int(p.deaths) == max_deaths]
    return {
        "players": [
            {"champion": p.champion_name, "deaths": int(p.deaths), "team": p.team}
            for p in tied
        ],
        "count": len(tied),
        "max_deaths": max_deaths,
    }


def find_player_by_champion_name(snapshot: GameSnapshot, champion_name: str, champion_catalog=None) -> PlayerInfo | None:
    """通过 champion_name / rawChampionName 查找玩家（使用 ChampionCatalog 解析）。"""
    if champion_catalog is None:
        from ..lol.data.champion_catalog import ChampionCatalog
        champion_catalog = ChampionCatalog()

    # 解析用户查询的英雄到 canonical cid
    query_champ = champion_catalog.get_by_name(champion_name)
    if query_champ is None:
        return None
    query_cid = query_champ.cid

    # 遍历所有玩家，解析每个玩家的英雄到 canonical cid
    for p in (snapshot.players or []):
        # 优先用 rawChampionName（英文名）
        player_champ = champion_catalog.resolve_player_champion(
            p.champion_name,
            getattr(p, 'raw_champion_name', '')
        )
        if player_champ and player_champ.cid == query_cid:
            return p
    return None


def champion_level(snapshot: GameSnapshot, champion_name: str, champion_catalog=None) -> dict[str, Any] | None:
    """查询某英雄等级。"""
    p = find_player_by_champion_name(snapshot, champion_name, champion_catalog)
    if p is None:
        return None
    return {
        "champion": p.champion_name,
        "level": p.level,
        "team": p.team,
    }


def champion_kda(snapshot: GameSnapshot, champion_name: str, champion_catalog=None) -> dict[str, Any] | None:
    """查询某英雄战绩。"""
    p = find_player_by_champion_name(snapshot, champion_name, champion_catalog)
    if p is None:
        return None
    return {
        "champion": p.champion_name,
        "kills": p.kills,
        "deaths": p.deaths,
        "assists": p.assists,
        "level": p.level,
    }


def self_level(snapshot: GameSnapshot) -> dict[str, Any] | None:
    """查询自己当前等级。"""
    cp = snapshot.current_player
    if cp is None:
        return None
    return {
        "champion": cp.champion_name,
        "level": cp.level,
    }
