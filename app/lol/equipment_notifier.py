"""
敌方成装更新语音提醒（v2）

需求（用户已确认）：
1. 每个敌人独立计算"第一件成装"（price > 2000 且非消耗品）→ 播报一次"对面XX出了YY"
2. 特殊四件（itemID 精确匹配）→ 每人每件额外播报一次
   探索者的护臂 2420 / 梅贾的窃魂卷 3041 / 中娅沙漏 3157 / 守护天使 3026
3. 若第一件就是特殊四件之一 → 合并成 1 条（不重复播报）
4. 每人每件只播一次（装备刷新不重复播）
5. 首帧静默：进游戏第一帧只记录不播报，杜绝轰炸
6. 只播敌方玩家

接线：main.py 订阅 game.time_update（每帧 ~1Hz）调 update(snapshot.players)；
game.ended 调 clear()。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_B

# 特殊四件 itemID（16.18.1 DataDragon zh_CN 实证）
# 2420 探索者的护臂 / 3041 梅贾的窃魂卷 / 3157 中娅沙漏 / 3026 守护天使
SPECIAL_IDS = {2420, 3041, 3157, 3026}

# 第一件成装的价格阈值
FIRST_ITEM_PRICE_THRESHOLD = 2000

# 敌方兜底 team（找不到我方队伍时沿用 live_equip_watcher 约定）
FALLBACK_ENEMY_TEAM = "CHAOS"


def _price_of(item: Dict[str, Any]) -> float:
    """安全取 item price；非数字/缺失返回 0"""
    try:
        v = item.get("price")
        if v is None:
            return 0.0
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _item_id_of(item: Dict[str, Any]) -> int:
    """安全取 itemID；缺失/非数字返回 0"""
    try:
        v = item.get("itemID")
        if v is None:
            return 0
        return int(v)
    except (TypeError, ValueError):
        return 0


def _item_name_of(item: Dict[str, Any]) -> str:
    """取装备显示名"""
    name = str(item.get("displayName") or item.get("name") or "")
    return name or "未知装备"


@dataclass
class PlayerEquipmentHistory:
    """单个玩家的装备播报状态"""
    first_announced: bool = False       # 是否已播"第一件成装"
    special_announced: Set[int] = field(default_factory=set)  # 已播过的特殊四件 itemID
    initialized: bool = False           # 是否已完成首帧静默记录


class EquipmentUpdateNotifier:
    """
    敌方成装更新语音提醒器（v2）

    工作流程：
    1. 每帧调用 update(players)（players 为 PlayerInfo 列表）
    2. 首帧静默：只记录每个玩家的初始状态
    3. 之后逐玩家检测：第一件 >2000 成装、特殊四件
    4. 有新增则合并推送到 SpeechQueue
    """

    def __init__(self, speech_queue: SpeechQueue):
        self.speech = speech_queue
        self._history: Dict[str, PlayerEquipmentHistory] = {}
        self._game_initialized: bool = False

    # ---------- 对外接口 ----------

    def update(self, players: List[Any]):
        """
        用最新玩家列表更新装备状态。

        Args:
            players: PlayerInfo 列表（也兼容 2999 dict 列表，字段用 getattr 归一化）
        """
        if not players:
            return

        # 首帧静默：只记录，不播报
        if not self._game_initialized:
            for p in players:
                key = self._player_key(p)
                if not key:
                    continue
                hist = self._history.setdefault(key, PlayerEquipmentHistory())
                # 记录首帧已有装备：
                #  - 已有特殊四件 → 标记"已见"，后续不重播旧装备
                #  - 已有 >2000 成装 → 标记 first_announced（第一件已"过"，不补播）
                # （防止对局中途启动软件时，把"早就出的装备"当新出的播报）
                items = getattr(p, "items", None) or []
                if isinstance(items, list):
                    for it in items:
                        if not isinstance(it, dict):
                            continue
                        iid = _item_id_of(it)
                        if iid in SPECIAL_IDS:
                            hist.special_announced.add(iid)
                    if any(
                        isinstance(it, dict)
                        and it.get("consumable", False) is False
                        and _price_of(it) > FIRST_ITEM_PRICE_THRESHOLD
                        for it in items
                    ):
                        hist.first_announced = True
            self._game_initialized = True
            return

        enemy_team = self._resolve_enemy_team(players)
        announcements: List[str] = []

        for p in players:
            team = getattr(p, "team", "") or ""
            if team != enemy_team:
                continue

            key = self._player_key(p)
            if not key:
                continue

            hist = self._history.setdefault(key, PlayerEquipmentHistory())
            hist.initialized = True

            items = getattr(p, "items", None) or []
            if not isinstance(items, list):
                continue

            champ = (
                getattr(p, "champion_name", None)
                or getattr(p, "summoner_name", None)
                or "敌人"
            )

            # ---- 规则 1：第一件成装（price > 2000 且非消耗品）----
            if not hist.first_announced:
                candidates = [
                    it for it in items
                    if isinstance(it, dict)
                    and it.get("consumable", False) is False
                    and _price_of(it) > FIRST_ITEM_PRICE_THRESHOLD
                ]
                if candidates:
                    # 近似"先买的"：取当前价格最低的成装
                    first = min(candidates, key=_price_of)
                    announcements.append(f"对面{champ}出了{_item_name_of(first)}")
                    hist.first_announced = True
                    iid = _item_id_of(first)
                    if iid in SPECIAL_IDS:
                        # 第一件就是特殊四件 → 合并，不额外播
                        hist.special_announced.add(iid)

            # ---- 规则 2：特殊四件（逐个检查，未播的额外播）----
            for it in items:
                if not isinstance(it, dict):
                    continue
                iid = _item_id_of(it)
                if iid in SPECIAL_IDS and iid not in hist.special_announced:
                    announcements.append(f"对面{champ}出了{_item_name_of(it)}")
                    hist.special_announced.add(iid)

        # 合并推送
        if announcements:
            self._push(announcements)

    def clear(self):
        """游戏结束时清空所有状态"""
        self._history.clear()
        self._game_initialized = False

    # ---------- 内部辅助 ----------

    @staticmethod
    def _player_key(p: Any) -> str:
        """玩家标识：优先 puuid（稳定），没有则用 champion_name"""
        puuid = str(getattr(p, "puuid", "") or "")
        if puuid:
            return f"puuid:{puuid}"
        champ = str(getattr(p, "champion_name", "") or "")
        if champ:
            return f"champ:{champ}"
        return ""

    @staticmethod
    def _resolve_enemy_team(players: List[Any]) -> str:
        """确定敌方队伍：找当前玩家（is_current_player）的 team，取反；找不到兜底 CHAOS"""
        for p in players:
            if getattr(p, "is_current_player", False):
                my_team = str(getattr(p, "team", "") or "")
                if my_team == "ORDER":
                    return "CHAOS"
                if my_team == "CHAOS":
                    return "ORDER"
                break
        return FALLBACK_ENEMY_TEAM

    def _push(self, announcements: List[str]):
        """合并成一条语音推送到 SpeechQueue（全部播，不丢数据）"""
        if len(announcements) == 1:
            text = announcements[0] + "。"
        else:
            text = "、".join(announcements) + "。"  # 全部播（一局最多几条，不怕多）

        item = SpeechItem(
            text=text,
            priority=PRIORITY_B,  # 低优先级
            expire_at=time.time() + 10.0,  # 10秒 TTL
            category="equipment",
            event_id=f"equip_{hash(text)}_{time.time()}",
        )
        self.speech.enqueue(item)
