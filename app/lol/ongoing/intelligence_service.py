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
    habitual_positions: List[str] = None  # ["UTILITY"] 等

    # B 行数据：近期英雄统计（analyze 产物，场次→KDA 排序）
    champion_stats: List[ChampionStat] = None

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

        # 2. 查询最近20场
        sgp = self._ensure_sgp()
        if sgp:
            try:
                res = sgp.recent(puuid, start_index=0, count=20)
                if res.state == "AVAILABLE" and res.records:
                    # 用 PlayerIntelligenceService 分析
                    intel_result = self._intelligence.analyze(
                        puuid=puuid,
                        matches=res.records,
                        current_champion_id=current_champion_id,
                        mastery_status=status,
                        mastery_top3=top3,
                    )
                    entry.recent_status = "AVAILABLE"
                    entry.recent_raw_games = intel_result.raw_games
                    entry.recent_games = intel_result.games
                    entry.recent_win_rate = intel_result.win_rate
                    entry.recent_average_kda = intel_result.average_kda
                    entry.habitual_positions = intel_result.habitual_positions
                    entry.champion_stats = intel_result.champion_stats
                else:
                    entry.recent_status = "EMPTY"
            except Exception as e:
                entry.recent_status = "UNAVAILABLE"
        else:
            entry.recent_status = "UNAVAILABLE"

        entry.queried_recent = True
        self._cache[puuid] = entry
        return entry

    def get_cached(self, puuid: str) -> Optional[PlayerIntelligenceCacheEntry]:
        """只读缓存命中（不触发任何请求）。未查询过返回 None。"""
        if not puuid:
            return None
        return self._cache.get(puuid)

    def clear(self):
        """游戏结束时清空缓存"""
        self._cache.clear()

    def __len__(self):
        return len(self._cache)





