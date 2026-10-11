"""
敌方成装更新语音提醒
基于现有 GameSnapshot 玩家 items 字段做 StateDiff
只播报新增的 Legendary/Mythic 成装，不播报组件
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from app.speech.speech_queue import SpeechQueue, SpeechItem, PRIORITY_B

# 成装 ID 集合（常见的传说/神话装备，26.18）
# 这里用简单的名称匹配，后续可以从 item JSON 加载
_COMPLETED_ITEM_KEYWORDS = {
    "日炎", "兰顿", "反甲", "无尽", "饮血", "三相", "无尽之刃",
    "斯塔缇克", "电刀", "无尽", "法爆", "帽子", "大帽", "灭世者",
    "虚空之杖", "法穿棒", "沙漏", "中娅", "女妖", "复活甲",
    "水银鞋", "明朗", "CD鞋", "法穿鞋", "护甲鞋", "魔抗鞋",
    "黑色切割", "黑切", "幕刃", "幽梦", "迅刃", "收集者",
    "贪欲", "破败", "纳什", "巫妖", "影焰", "暗影",
}

# 组件关键词（这些不播报）
_COMPONENT_KEYWORDS = {
    "长剑", "十字镐", "红水晶", "布甲", "抗魔斗篷", "草鞋",
    "匕首", "短剑", "蓝水晶", "增幅", "仙女护符", "治疗宝珠",
}


def _is_completed_item(item: Dict[str, Any]) -> bool:
    """判断是否是完成的成装（不是组件/鞋子）"""
    name = str(item.get("displayName") or item.get("name") or "")
    if not name:
        return False
    # 排除组件
    for comp in _COMPONENT_KEYWORDS:
        if comp in name:
            return False
    # 匹配成装关键词
    for kw in _COMPLETED_ITEM_KEYWORDS:
        if kw in name:
            return True
    # 如果价格足够高，也算成装（兜底）
    price = item.get("price") or item.get("totalPrice") or 0
    try:
        if int(price) >= 1300:
            return True
    except (TypeError, ValueError):
        pass
    return False


def _get_completed_items(items: List[Dict[str, Any]]) -> Set[str]:
    """从 items 列表提取成装名称集合"""
    result = set()
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if _is_completed_item(item):
            name = str(item.get("displayName") or item.get("name") or "")
            if name:
                result.add(name)
    return result


@dataclass
class PlayerEquipmentHistory:
    """单个玩家的装备历史"""
    puuid: str
    last_completed: Set[str] = None  # 上次已知的成装集合
    initialized: bool = False        # 是否第一次初始化完成

    def __post_init__(self):
        if self.last_completed is None:
            self.last_completed = set()


class EquipmentUpdateNotifier:
    """
    敌方成装更新语音提醒器
    工作流程：
    1. 每次 snapshot 更新时调用 update(players)
    2. 对比每个玩家的当前成装和历史成装
    3. 有新增成装则生成语音文本
    4. 推送到 SpeechQueue
    """

    def __init__(self, speech_queue: SpeechQueue):
        self.speech = speech_queue
        self._history: Dict[str, PlayerEquipmentHistory] = {}  # puuid -> history

    def update(self, players: List[Dict[str, Any]]):
        """
        用最新的玩家列表更新装备状态，检测新增成装。

        Args:
            players: 2999 playerlist 或 allPlayers 列表，每个元素必须有：
                - puuid 或 summonerName 作为身份键
                - items: 装备列表
        """
        new_completions: List[str] = []  # 要播报的文本列表

        for p in players:
            # 用 puuid 作为身份键，没有就用 summonerName
            identity = str(p.get("puuid") or p.get("summonerName") or "")
            if not identity:
                continue

            current_items = p.get("items") or []
            current_completed = _get_completed_items(current_items)

            # 获取历史记录
            if identity not in self._history:
                self._history[identity] = PlayerEquipmentHistory(puuid=identity)
            history = self._history[identity]

            # 计算新增的成装（不管是不是第一次，只要是新的就播报）
            new_items = current_completed - history.last_completed
            if new_items:
                # 获取英雄名
                champ = p.get("championName") or p.get("summonerName") or "敌人"
                for item_name in sorted(new_items):
                    new_completions.append(f"{champ}更新了{item_name}")

            # 更新历史
            history.last_completed = current_completed
            history.initialized = True

        # 推送到 SpeechQueue
        if new_completions:
            # 合并成一条语音，避免轰炸
            if len(new_completions) == 1:
                text = new_completions[0] + "。"
            else:
                text = "，".join(new_completions[:2]) + "。"  # 最多播报2个

            item = SpeechItem(
                text=text,
                priority=PRIORITY_B,  # 低优先级
                expire_at=__import__('time').time() + 10.0,  # 10秒TTL
                category="equipment",
                event_id=f"equip_{hash(text)}_{__import__('time').time()}",
            )
            self.speech.enqueue(item)

    def clear(self):
        """游戏结束时清空所有历史"""
        self._history.clear()
