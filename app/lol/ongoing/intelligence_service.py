"""P.4.5/P.4.6 当前对局玩家情报聚合器。

负责：
- 为当前对局中的每个玩家查询 Champion Mastery
- 为每个玩家查询最近 20 场战绩
- 缓存结果，避免重复请求
- 不直接访问 UI，只提供数据

数据链路：
GameSnapshot.PlayerInfo.puuid
    ↓
ChampionMasteryProvider.get_top3(puuid)  → Mastery Top3
SgpMatchHistoryProvider.recent(puuid, 20)  → 最近20场
PlayerIntelligenceService.analyze(...)     → 胜率/KDA
    ↓
缓存（按 puuid，当前游戏生命周期内）
    ↓
UI ViewModel
"""
from __future__ import annotations

import time
from typing import Dict, Optional, List
from dataclasses import dataclass, field

from .champion_mastery import ChampionMasteryProvider, ChampionMasteryRecord, AVAILABLE
from .intelligence import PlayerIntelligenceService, PlayerIntelligenceResult, ChampionStat
from .match_model import MatchRecord


@dataclass
class PlayerIntelligenceCacheEntry:
    """单个玩家的情报缓存"""
    puuid: str

    # Mastery
    mastery_status: str = ""
    mastery_top3: List[ChampionMasteryRecord] = None
    current_champion_is_mastery_top3: Optional[bool] = None

    # 最近20场
    recent_status: str = ""  # AVAILABLE / EMPTY / UNAVAILABLE / NO_PUUID
    recent_games: int = 0          # 有效比赛数
    recent_raw_games: int = 0      # 原始查询数
    recent_win_rate: float = 0.0
    recent_average_kda: float = 0.0
    recent_wins: int = 0           # 有胜负结果场次的胜数
    recent_losses: int = 0         # 有胜负结果场次的负数
    recent_unknown_count: int = 0  # win 缺失场次数（不计胜负）
    habitual_positions: List[str] = None  # ["UTILITY"] 等

    # B 行数据：近期英雄统计（analyze 产物，场次→KDA 排序）
    champion_stats: List[ChampionStat] = None

    # 称号判定：连胜/连败 + 辅助占比
    streak: int = 0
    utility_ratio: float = 0.0

    # 近 10 场窗口（档 1/2 用）
    games_10: int = 0
    win_rate_10: float = 0.0
    average_kda_10: float = 0.0
    streak_10: int = 0

    queried_mastery: bool = False
    queried_recent: bool = False


class OngoingIntelligenceService:
    """当前对局玩家情报服务。

    职责：
    - 为每个玩家查询 Mastery Top3
    - 为每个玩家查询最近20场战绩
    - 内存缓存（同一次游戏内不重复请求）
    - 游戏结束时清空
    """

    def __init__(
        self,
        mastery_provider: ChampionMasteryProvider,
        sgp_provider=None,  # SgpMatchHistoryProvider
        intelligence_service: PlayerIntelligenceService = None,
    ):
        self._mastery = mastery_provider
        self._sgp = sgp_provider
        self._sgp_factory = None  # 动态创建 SGP provider 的回调
        self._intelligence = intelligence_service or PlayerIntelligenceService()
        self._cache: Dict[str, PlayerIntelligenceCacheEntry] = {}
        self._last_fail: Dict[str, float] = {}  # puuid -> 失败时间戳（30s 节流防风暴）

    def set_sgp_factory(self, factory):
        """注入动态创建 SGP provider 的回调（解决 LCU 后连问题）。"""
        self._sgp_factory = factory

    def _ensure_sgp(self):
        """确保 SGP provider 可用，如果初始失败则动态重试。"""
        if self._sgp is None and self._sgp_factory:
            self._sgp = self._sgp_factory()
        return self._sgp

    def get_player_intelligence(
        self,
        puuid: str,
        current_champion_id: int = 0,
    ) -> PlayerIntelligenceCacheEntry:
        """
        获取单个玩家的情报。
        如果已缓存，直接返回；否则查询 Mastery + 最近20场。
        """
        if not puuid:
            return PlayerIntelligenceCacheEntry(puuid="", mastery_status="NO_PUUID", recent_status="NO_PUUID")

        # 命中缓存
        if puuid in self._cache:
            entry = self._cache[puuid]
            # 更新当前英雄判断（如果当前英雄变了）
            if entry.queried_mastery and entry.mastery_status == AVAILABLE and current_champion_id > 0:
                entry.current_champion_is_mastery_top3 = any(
                    m.champion_id == current_champion_id
                    for m in (entry.mastery_top3 or [])
                )
            return entry

        # 创建新条目
        entry = PlayerIntelligenceCacheEntry(puuid=puuid)

        # 1. 查询 Mastery（独立 try except，失败不影响 SGP）
        try:
            status, top3 = self._mastery.get_top3(puuid)
            entry.mastery_status = status
            entry.mastery_top3 = top3
            entry.queried_mastery = True

            if status == AVAILABLE and top3 and current_champion_id > 0:
                entry.current_champion_is_mastery_top3 = any(
                    m.champion_id == current_champion_id
                    for m in top3
                )
        except Exception as e:
            entry.mastery_status = "UNAVAILABLE"
            entry.mastery_top3 = []
            entry.queried_mastery = True

        # 2. 查询最近50场（候选 A：窗口与 loader 对齐，显示/判定同源）
        sgp = self._ensure_sgp()
        if sgp:
            try:
                res = sgp.recent(puuid, start_index=0, count=50)
                if res.state == "AVAILABLE" and res.records:
                    # 用 PlayerIntelligenceService 分析
                    intel_result = self._intelligence.analyze(
                        puuid=puuid,
                        matches=res.records,
                        current_champion_id=current_champion_id,
                        mastery_status=status,
                        mastery_top3=top3,
                    )
                    # 有效比赛数 ≥1 才标 AVAILABLE；0 有效场（训练营/自定义全被 PVE
                    # 过滤）→ EMPTY：不评称号，避免 0 数据进 compute_title 落空
                    if intel_result.games >= 1:
                        entry.recent_status = "AVAILABLE"
                        entry.recent_raw_games = intel_result.raw_games
                        entry.recent_games = intel_result.games
                        entry.recent_win_rate = intel_result.win_rate
                        entry.recent_average_kda = intel_result.average_kda
                        entry.recent_wins = intel_result.wins
                        entry.recent_losses = intel_result.losses
                        entry.recent_unknown_count = intel_result.unknown_count
                        entry.habitual_positions = intel_result.habitual_positions
                        entry.champion_stats = intel_result.champion_stats
                        entry.streak = intel_result.streak
                        entry.utility_ratio = intel_result.utility_ratio
                        entry.games_10 = intel_result.games_10
                        entry.win_rate_10 = intel_result.win_rate_10
                        entry.average_kda_10 = intel_result.average_kda_10
                        entry.streak_10 = intel_result.streak_10
                    else:
                        entry.recent_status = "EMPTY"
                else:
                    entry.recent_status = "EMPTY"
            except Exception as e:
                entry.recent_status = "UNAVAILABLE"
        else:
            entry.recent_status = "UNAVAILABLE"

        entry.queried_recent = True
        if entry.recent_status in ("UNAVAILABLE", "EMPTY"):
            # 失败：不写缓存（否则一次性失败被永久缓存，永不重试）+ 记时间戳节流
            self._last_fail[puuid] = time.monotonic()
        else:
            self._cache[puuid] = entry
            self._last_fail.pop(puuid, None)  # 成功：写缓存 + 清失败时间戳
        return entry

    def should_throttle(self, puuid: str, window: float = 30.0) -> bool:
        """失败节流：最近失败在 window 秒内返回 True（不重试），防止每秒风暴。"""
        return time.monotonic() - self._last_fail.get(puuid, 0.0) < window

    def get_cached(self, puuid: str) -> Optional[PlayerIntelligenceCacheEntry]:
        """只读缓存命中（不触发任何请求）。未查询过返回 None。"""
        if not puuid:
            return None
        return self._cache.get(puuid)

    def clear(self):
        """游戏结束时清空缓存"""
        self._cache.clear()
        self._last_fail.clear()

    def __len__(self):
        return len(self._cache)





