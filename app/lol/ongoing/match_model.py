"""统一账号战绩模型 + 可用性状态。

设计参考 LeagueAkari 的统一 MatchHistory Model 思想：
SGP 与 LCU 两种来源最终归一到同一个 MatchRecord，
UI/Service 只消费本模型，不感知来源差异。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


# 可用性状态（严格区分“真实 0 胜”与“拿不到”）
AVAILABLE = "AVAILABLE"
NO_DATA = "NO_DATA"
UNSUPPORTED = "UNSUPPORTED"
AUTH_REQUIRED = "AUTH_REQUIRED"
SGP_UNAVAILABLE = "SGP_UNAVAILABLE"
ERROR = "ERROR"


# 计入统计的队列白名单（唯一权威口径，intelligence 与 loader 共用）：
#   400 匹配 / 430 匹配(盲选) / 460·490 匹配变体 / 420 单双排 / 440 灵活排位
# 排除：470(双人排位) / 0(自定义) / 880(未知娱乐变体) / 全部 PVE·特殊模式
_VALID_QUEUE_IDS = {400, 420, 430, 440, 460, 490}


@dataclass
class MatchRecord:
    game_id: int = 0
    game_creation: int = 0          # epoch ms
    game_duration: int = 0          # seconds
    queue_id: int = 0
    queue_type: str = ""             # SGP/LCU 解析出的队列名（可空）
    champion_id: int = 0
    champion_name: str = ""
    win: Optional[bool] = None       # None=未知，True/False=确定
    kills: Optional[int] = None
    deaths: Optional[int] = None
    assists: Optional[int] = None
    cs: Optional[int] = None         # totalMinionsKilled + neutralMinionsKilled
    gold: Optional[int] = None
    team_position: str = ""
    team_id: int = 0
    source: str = "unknown"          # sgp / lcu


@dataclass
class MatchHistoryResult:
    state: str = ERROR               # AVAILABLE / NO_DATA / ...
    records: List[MatchRecord] = field(default_factory=list)
    total: int = 0                   # 服务端声称的总数（SGP 可远大于 LCU 20）
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.state == AVAILABLE
