"""P.4.3 Champion Mastery Provider：从 LCU 读取玩家英雄熟练度。

数据来源：GET /lol-champion-mastery/v1/{puuid}/champion-mastery
用途：Top 3 熟练英雄、当前英雄是否属于熟练英雄
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

# 状态常量
AVAILABLE = "AVAILABLE"
EMPTY = "EMPTY"
UNAVAILABLE = "UNAVAILABLE"
NO_PUUID = "NO_PUUID"


@dataclass
class ChampionMasteryRecord:
    """单个英雄的熟练度记录"""
    champion_id: int
    champion_level: int = 0
    champion_points: int = 0
    highest_grade: str = ""
    last_play_time: int = 0


class ChampionMasteryProvider:
    """Champion Mastery 查询 Provider。
    
    只接受 puuid，不自己解析身份。
    使用注入的 lcu_get callable，便于测试和复用现有 LCU 连接。
    """

    def __init__(self, lcu_get: Callable):
        self._lcu_get = lcu_get

    def get_masteries(self, puuid: str) -> tuple[str, list[ChampionMasteryRecord]]:
        """
        查询玩家所有英雄熟练度。
        
        Args:
            puuid: 玩家 PUUID
            
        Returns:
            (status, records) 元组
            status: AVAILABLE / EMPTY / UNAVAILABLE / NO_PUUID
            records: ChampionMasteryRecord 列表（已按 level DESC, points DESC 排序）
        """
        if not puuid:
            return NO_PUUID, []

        try:
            url = f"/lol-champion-mastery/v1/{puuid}/champion-mastery"
            result = self._lcu_get(url)
        except Exception:
            return UNAVAILABLE, []

        if not isinstance(result, list):
            return UNAVAILABLE, []

        if not result:
            return EMPTY, []

        # 解析记录
        records = []
        for item in result:
            try:
                rec = ChampionMasteryRecord(
                    champion_id=int(item.get("championId", 0)),
                    champion_level=int(item.get("championLevel", 0)),
                    champion_points=int(item.get("championPoints", 0)),
                    highest_grade=str(item.get("highestGrade", "")),
                    last_play_time=int(item.get("lastPlayTime", 0)),
                )
                if rec.champion_id > 0:
                    records.append(rec)
            except (ValueError, TypeError):
                continue

        if not records:
            return EMPTY, []

        # 显式排序：level DESC, points DESC, champion_id ASC
        records.sort(key=lambda r: (-r.champion_level, -r.champion_points, r.champion_id))

        return AVAILABLE, records

    def get_top3(self, puuid: str) -> tuple[str, list[ChampionMasteryRecord]]:
        """
        获取 Top 3 熟练英雄。
        
        Returns:
            (status, top3_records)
        """
        status, all_records = self.get_masteries(puuid)
        if status != AVAILABLE:
            return status, []
        return status, all_records[:3]
