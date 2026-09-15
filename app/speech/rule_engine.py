"""
Rule Engine - 确定性游戏事件规则引擎

职责：
- 从 Domain Event 提取规则匹配
- 产生 SpeechCandidate（候选播报）
- 提供 priority_hint、category、text template

不负责：
- 最终是否播报（SpeechJudge 决定）
- 直接调用 TTS / Queue
- LLM / 网络请求
- Kill 聚合（KillAggregator 负责）

原则：
- Read > Infer
- 确定性规则，不用 AI
- 轻量同步，毫秒级完成
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..core.event_bus import Event, EventType


@dataclass
class SpeechCandidate:
    """
    语音播报候选

    由 RuleEngine 产生，交给 SpeechJudge 做最终决定。
    """
    rule_id: str
    event_id: str
    event_type: str
    category: str
    text: str
    priority_hint: str  # "S" / "A" / "B" / "DROP"
    created_at: float = field(default_factory=time.time)
    expire_at: float = 0.0
    interruptible: bool = True
    metadata: Dict[str, Any] = field(default_factory=dict)


class RuleEngine:
    """
    确定性规则引擎

    把 Domain Event 映射为 SpeechCandidate。
    所有规则是确定性的，不使用 AI。
    """

    # 事件规则表：event_type → (rule_id, category, priority_hint, text_template)
    RULES = {
        # 游戏开始/结束
        EventType.GAME_STARTED: (
            "game_started",
            "game",
            "B",
            "游戏开始"
        ),
        EventType.GAME_ENDED: (
            "game_ended",
            "game",
            "A",
            "游戏结束"
        ),

        # 击杀
        EventType.CHAMPION_KILL: (
            "champion_kill",
            "kill",
            "B",
            "你击杀了敌方英雄"
        ),
        EventType.MULTI_KILL: (
            "multi_kill",
            "kill",
            "A",
            "多杀"
        ),
        EventType.FIRST_BLOOD: (
            "first_blood",
            "kill",
            "A",
            "第一滴血"
        ),
        EventType.ACE: (
            "ace",
            "kill",
            "A",
            "团灭"
        ),

        # 目标
        EventType.DRAGON_KILLED: (
            "dragon_killed",
            "objective",
            "A",
            "龙被击杀"
        ),
        EventType.BARON_KILLED: (
            "baron_killed",
            "objective",
            "S",
            "男爵被击杀"
        ),
        EventType.HERALD_KILLED: (
            "herald_killed",
            "objective",
            "A",
            "先驱被击杀"
        ),
        EventType.TOWER_DESTROYED: (
            "tower_destroyed",
            "objective",
            "A",
            "防御塔被摧毁"
        ),
        EventType.INHIBITOR_DESTROYED: (
            "inhibitor_destroyed",
            "objective",
            "A",
            "水晶被摧毁"
        ),

        # 提醒
        EventType.ALARM_TRIGGERED: (
            "alarm_triggered",
            "alarm",
            "B",
            "技能就绪"
        ),
    }

    # C/DROP 级事件（不产生 candidate）
    C_LEVEL_EVENTS = {
        EventType.PLAYER_LEVEL_UP,
        EventType.PLAYER_ITEM_CHANGED,
        EventType.GAME_TIME_UPDATE,
        EventType.MUSIC_TRACK_CHANGED,
        EventType.ALARM_CREATED,
    }

    def __init__(self):
        # 已处理的 event_id 去重
        self._seen_event_ids: set = set()

    def evaluate(self, event: Event) -> Optional[SpeechCandidate]:
        """
        评估事件，返回 SpeechCandidate 或 None

        Args:
            event: Domain Event

        Returns:
            SpeechCandidate 或 None（DROP）
        """
        # C 级事件直接 DROP
        if event.event_type in self.C_LEVEL_EVENTS:
            return None

        # 查找规则
        rule = self.RULES.get(event.event_type)
        if not rule:
            return None

        rule_id, category, priority_hint, text_template = rule

        # 生成稳定 event_id（用于 dedup）
        event_id = self._make_event_id(event)

        # dedup
        if event_id in self._seen_event_ids:
            return None
        self._seen_event_ids.add(event_id)

        # 生成文本（从 payload 填充模板）
        text = self._render_text(text_template, event)

        # TTL（根据优先级 hint）
        ttl_map = {"S": 10.0, "A": 6.0, "B": 4.0}
        ttl = ttl_map.get(priority_hint, 4.0)

        return SpeechCandidate(
            rule_id=rule_id,
            event_id=event_id,
            event_type=event.event_type,
            category=category,
            text=text,
            priority_hint=priority_hint,
            expire_at=time.time() + ttl,
            interruptible=True,
            metadata={
                "game_time": event.get("game_time"),
            },
        )

    def _make_event_id(self, event: Event) -> str:
        """
        生成稳定 event_id（用于 dedup）

        优先级：
        1. Riot 原生 EventID（riot_event_id）
        2. payload 中的 event_id
        3. fallback：event_type + 关键 payload 字段
        """
        # 第一优先级：Riot 原生 EventID
        riot_eid = event.get("riot_event_id")
        if riot_eid:
            return f"{event.event_type}:riot_{riot_eid}"

        # 第二优先级：payload 中的 event_id
        payload_eid = event.get("event_id")
        if payload_eid:
            return f"{event.event_type}:{payload_eid}"

        # Fallback：event_type + 关键 payload 字段
        game_time = event.get("game_time", 0)
        killer = event.get("killer_name", "")
        victim = event.get("victim_name", "")

        # 组合稳定字段
        stable_parts = [event.event_type]
        if game_time:
            stable_parts.append(str(game_time))
        if killer:
            stable_parts.append(killer)
        if victim:
            stable_parts.append(victim)

        return ":".join(stable_parts)

    def _render_text(self, template: str, event: Event) -> str:
        """
        从模板和 payload 生成实际文本

        V1 简单实现：直接返回模板，或从 payload 填充基本字段。
        """
        text = template

        # 尝试填充一些基本字段
        killer = event.get("killer_name")
        if killer and "{killer}" in template:
            text = text.replace("{killer}", killer)

        victim = event.get("victim_name")
        if victim and "{victim}" in template:
            text = text.replace("{victim}", victim)

        count = event.get("count")
        if count and "{count}" in template:
            text = text.replace("{count}", str(count))

        return text

    def clear(self):
        """清除 dedup 状态（新游戏开始时调用）"""
        self._seen_event_ids.clear()

    def get_seen_count(self) -> int:
        """已处理的 event_id 数量"""
        return len(self._seen_event_ids)
