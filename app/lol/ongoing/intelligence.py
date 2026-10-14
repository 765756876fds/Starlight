"""P.4.1 玩家智能分析层：从现有历史比赛数据计算统计指标。

输入：PlayerSearchResult（已有 SGP 历史数据）
输出：PlayerIntelligenceResult（聚合统计 + 英雄统计）

设计原则：
- 纯计算层，不发起新的网络请求
- 只消费现有 MatchRecord 数据
- 不做主观评价，只输出事实统计
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict

from .match_model import MatchRecord, _VALID_QUEUE_IDS
from .champion_mastery import ChampionMasteryRecord


def _is_valid_match(m: MatchRecord) -> bool:
    """判断是否为有效比赛（只算匹配/单双排/灵活排位，其余全排除）。

    候选 A：不再要求 win/KDA 非 None——字段缺失场次进入统计但"中性剔除"
    （win 缺失不计胜负、KDA 缺失不参与平均），保证显示窗口与称号判定
    窗口一致（探针实测缺失率 0%，此处为防御）。
    """
    # 白名单：非 {400,420,430,440,460,490} 一律排除（自定义/人机/大乱斗/娱乐/未知）
    if m.queue_id not in _VALID_QUEUE_IDS:
        return False
    return True


@dataclass
class ChampionStat:
    """单个英雄的统计"""
    champion_id: int
    champion_name: str
    games: int = 0
    wins: int = 0
    losses: int = 0
    total_kills: int = 0
    total_deaths: int = 0
    total_assists: int = 0
    average_kda: float = 0.0
    win_rate: float = 0.0

    def finalize(self):
        """计算派生指标"""
        if self.games > 0:
            self.win_rate = self.wins / self.games
            # 逐场 KDA 平均值在这里简化为总 K+A / 总死亡
            # 注：SUMMARY 接口没有逐场 KDA，只能用总量计算
            self.average_kda = (self.total_kills + self.total_assists) / max(self.total_deaths, 1)


@dataclass
class PlayerIntelligenceResult:
    """玩家智能分析结果"""
    puuid: str = ""
    game_name: str = ""
    tag_line: str = ""
    privacy: str = ""

    # 近期状态（来自 SGP 最近 20 场）
    games: int = 0               # 有效比赛数（queue 白名单后，含数据不全场次）
    raw_games: int = 0            # 原始总比赛数（未过滤）
    wins: int = 0
    losses: int = 0
    win_rate: float = 0.0
    unknown_count: int = 0        # win 缺失场次数（不计胜负；KDA 缺失不计入此字段）

    # 总数据
    total_kills: int = 0
    total_deaths: int = 0
    total_assists: int = 0
    average_kda: float = 0.0     # 逐场 KDA 的平均值

    # 近期英雄统计（来自历史比赛聚合）
    champion_stats: List[ChampionStat] = field(default_factory=list)

    # 常用位置（来自历史比赛 teamPosition 统计）
    habitual_positions: List[str] = field(default_factory=list)  # ["UTILITY", "MIDDLE"] 等

    # 近期形态（称号判定用）
    streak: int = 0               # 20 场窗口：最新→最旧连续胜负：>0 连胜, <0 连败
    utility_ratio: float = 0.0    # 近 20 场 UTILITY 占比（0~1，辅助判定用）

    # 近 10 场窗口（档 1/2 请神/天人合一判定，<5 场不评）
    games_10: int = 0
    win_rate_10: float = 0.0
    average_kda_10: float = 0.0
    streak_10: int = 0

    # Champion Mastery Top 3（来自 LCU Champion Mastery API）
    mastery_status: str = ""     # AVAILABLE / EMPTY / UNAVAILABLE / NO_PUUID
    mastery_top3: List[ChampionMasteryRecord] = field(default_factory=list)

    # 当前英雄判断
    current_champion_id: int = 0
    current_champion_games: int = 0
    current_champion_in_recent_history: bool = False
    current_champion_is_mastery_top3: Optional[bool] = None  # True/False/None(未知)

    # 状态
    valid: bool = False
    message: str = ""


def _per_game_kda(m: MatchRecord) -> float:
    """计算单场 KDA：(K+A) / max(D, 1)"""
    k = m.kills or 0
    a = m.assists or 0
    d = m.deaths or 0
    return (k + a) / max(d, 1)


class PlayerIntelligenceService:
    """玩家智能分析服务：纯计算，不发请求"""

    def analyze(
        self,
        puuid: str,
        matches: List[MatchRecord],
        game_name: str = "",
        tag_line: str = "",
        privacy: str = "",
        current_champion_id: int = 0,
        mastery_status: str = "",
        mastery_top3: Optional[List[ChampionMasteryRecord]] = None,
    ) -> PlayerIntelligenceResult:
        """
        分析玩家历史比赛数据。

        Args:
            puuid: 玩家 PUUID
            matches: MatchRecord 列表（来自 PlayerSearchResult.matches）
            game_name: 召唤师名
            tag_line: 标签
            privacy: 隐私状态
            current_champion_id: 当前对局英雄 ID（0=未知）
            mastery_status: Champion Mastery 状态
            mastery_top3: Champion Mastery Top 3 列表

        Returns:
            PlayerIntelligenceResult 聚合统计
        """
        result = PlayerIntelligenceResult(
            puuid=puuid,
            game_name=game_name,
            tag_line=tag_line,
            privacy=privacy,
            current_champion_id=current_champion_id,
            mastery_status=mastery_status,
            mastery_top3=mastery_top3 or [],
        )

        # 当前英雄是否属于 Champion Mastery Top 3（独立于 matches，先算）
        if current_champion_id > 0 and mastery_status == "AVAILABLE" and result.mastery_top3:
            result.current_champion_is_mastery_top3 = any(
                m.champion_id == current_champion_id
                for m in result.mastery_top3
            )

        if not matches:
            result.message = "no_matches"
            return result

        result.raw_games = len(matches)

        # 过滤有效比赛（按时间降序：最新在前，供连胜/连败判定）
        valid_matches = sorted(
            (m for m in matches if _is_valid_match(m)),
            key=lambda m: m.game_creation,
            reverse=True,
        )

        if not valid_matches:
            result.message = "no_valid_matches"
            return result

        result.games = len(valid_matches)

        # 近期形态：连胜/连败（最新→最旧连续胜负）
        if valid_matches and valid_matches[0].win is not None:
            first_win = valid_matches[0].win
            n = 0
            for m in valid_matches:
                if m.win is None or m.win != first_win:
                    break
                n += 1
            result.streak = n if first_win else -n

        # 近期形态：辅助占比（UTILITY 场次 / 有效场次）
        util_count = sum(
            1 for m in valid_matches
            if (m.team_position or "").upper() == "UTILITY"
        )
        result.utility_ratio = util_count / len(valid_matches)

        # 近 10 场窗口（档 1/2 用）：胜率 / KDA / 连胜
        recent10 = valid_matches[:10]
        result.games_10 = len(recent10)
        if recent10:
            w10 = sum(1 for m in recent10 if m.win)
            l10 = sum(1 for m in recent10 if m.win is False)
            result.win_rate_10 = w10 / (w10 + l10) if (w10 + l10) else 0.0
            kda10_list = [
                _per_game_kda(m) for m in recent10
                if m.kills is not None and m.deaths is not None and m.assists is not None
            ]
            result.average_kda_10 = sum(kda10_list) / len(kda10_list) if kda10_list else 0.0
            if recent10[0].win is not None:
                first_win = recent10[0].win
                n = 0
                for m in recent10:
                    if m.win is None or m.win != first_win:
                        break
                    n += 1
                result.streak_10 = n if first_win else -n

        # 总统计
        kda_list = []
        champion_map: Dict[int, ChampionStat] = {}

        for m in valid_matches:
            # 数据完整性：KDA 缺失场次不参与平均（不计入 unknown_count）
            kda_complete = (
                m.kills is not None
                and m.deaths is not None
                and m.assists is not None
            )
            # 胜负数据不全（win=None）：不计胜负，仅计数（显示"近N场 X胜Y负（N场数据不全）"自洽）
            if m.win is None:
                result.unknown_count += 1

            # 总击杀/死亡/助攻
            result.total_kills += m.kills or 0
            result.total_deaths += m.deaths or 0
            result.total_assists += m.assists or 0

            # 胜负（win=None 不计胜负，已计入 unknown_count）
            if m.win:
                result.wins += 1
            elif m.win is False:
                result.losses += 1

            # 逐场 KDA（缺失不参与平均）
            if kda_complete:
                kda_list.append(_per_game_kda(m))

            # 英雄统计
            cid = m.champion_id
            if cid not in champion_map:
                champion_map[cid] = ChampionStat(
                    champion_id=cid,
                    champion_name=m.champion_name,
                )
            cs = champion_map[cid]
            cs.games += 1
            if m.win:
                cs.wins += 1
            elif m.win is False:
                cs.losses += 1
            cs.total_kills += m.kills or 0
            cs.total_deaths += m.deaths or 0
            cs.total_assists += m.assists or 0

        # 胜率（分母 = 有胜负结果的场次数，与显示"近N场 X胜Y负"同源）
        decided = result.wins + result.losses
        result.win_rate = result.wins / decided if decided else 0.0

        # 平均 KDA（逐场平均值，只统计 KDA 完整场次）
        result.average_kda = sum(kda_list) / len(kda_list) if kda_list else 0.0

        # 英雄统计排序：场次 DESC, KDA DESC, champion_id ASC
        for cs in champion_map.values():
            cs.finalize()

        result.champion_stats = sorted(
            champion_map.values(),
            key=lambda c: (-c.games, -c.average_kda, c.champion_id)
        )

        # 常用位置统计
        valid_positions = {"TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"}
        position_counter: Dict[str, int] = {}
        for m in valid_matches:
            pos = (m.team_position or "").upper()
            if pos in valid_positions:
                position_counter[pos] = position_counter.get(pos, 0) + 1

        if position_counter:
            max_count = max(position_counter.values())
            # 按固定位置顺序排序并列结果
            position_order = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"]
            result.habitual_positions = [
                pos for pos in position_order
                if position_counter.get(pos, 0) == max_count
            ]

        # 当前英雄判断
        if current_champion_id > 0:
            for cs in result.champion_stats:
                if cs.champion_id == current_champion_id:
                    result.current_champion_games = cs.games
                    result.current_champion_in_recent_history = True
                    break

            # 当前英雄是否属于 Champion Mastery Top 3
            if mastery_status == "AVAILABLE" and result.mastery_top3:
                result.current_champion_is_mastery_top3 = any(
                    m.champion_id == current_champion_id
                    for m in result.mastery_top3
                )

        result.valid = True
        result.message = "ok"
        return result
