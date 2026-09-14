"""
Speech Judge - 播报控制

所有事件不能直接说话，必须经过 Speech Judge 评判。
等级：S / A / B / C / DROP

核心原则：信息可以丰富，语音必须克制。
监控可以详细，播报必须稀疏。
"""
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.event_bus import EventBus, Event, EventType, get_event_bus
from .kill_aggregator import KillAggregator
from .speech_queue import SpeechItem, PRIORITY_S, PRIORITY_A, PRIORITY_B


# 等级 → 数字优先级映射
LEVEL_TO_PRIORITY = {
    "S": PRIORITY_S,
    "A": PRIORITY_A,
    "B": PRIORITY_B,
}

# 默认 TTL
DEFAULT_TTL = {
    "S": 10.0,
    "A": 6.0,
    "B": 4.0,
}

# category TTL 覆盖
CATEGORY_TTL = {
    "death": 15.0,
    "objective": 8.0,
}


class SpeechLevel:
    """播报等级"""
    S = "S"       # 极高价值：关键目标、关键危险
    A = "A"       # 高价值：一血、团战结果、多杀
    B = "B"       # 普通：普通击杀、普通提醒、计时器
    C = "C"       # 低价值：只在 UI 显示
    DROP = "DROP"  # 完全不说


class SpeechJudge:
    """
    语音播报评判器

    决定哪些事件需要语音播报，哪些只在 UI 显示。
    """

    # 事件 -> 等级映射
    EVENT_LEVELS = {
        # S 级 - 极高价值
        EventType.BARON_KILLED: SpeechLevel.S,
        "game.nexus_destroyed": SpeechLevel.S,
        "game.team_wiped": SpeechLevel.S,

        # A 级 - 高价值
        EventType.DRAGON_KILLED: SpeechLevel.A,
        EventType.HERALD_KILLED: SpeechLevel.A,
        EventType.FIRST_BLOOD: SpeechLevel.A,
        EventType.MULTI_KILL: SpeechLevel.A,
        EventType.ACE: SpeechLevel.A,
        EventType.TOWER_DESTROYED: SpeechLevel.A,
        EventType.INHIBITOR_DESTROYED: SpeechLevel.A,

        # B 级 - 普通
        EventType.CHAMPION_KILL: SpeechLevel.B,
        EventType.ALARM_TRIGGERED: SpeechLevel.B,
        EventType.PLAYER_SUMMONER_SPELL_USED: SpeechLevel.B,

        # C 级 - 只在 UI 显示
        EventType.PLAYER_LEVEL_UP: SpeechLevel.C,
        EventType.PLAYER_ITEM_CHANGED: SpeechLevel.C,
        EventType.GAME_TIME_UPDATE: SpeechLevel.C,
        EventType.MUSIC_TRACK_CHANGED: SpeechLevel.C,
        EventType.ALARM_CREATED: SpeechLevel.C,
    }

    def __init__(self, config: Dict[str, Any], queue=None):
        self.config = config
        self.intensity = config.get("intensity", "sparse")
        self.min_level = config.get("min_level", "B")
        self.min_interval = config.get("min_interval", 5.0)

        self.event_bus = get_event_bus()
        self.kill_aggregator = KillAggregator(
            window=config.get("kill_aggregate_window", 3.0)
        )

        # SpeechQueue（可选，传入则直接入队；否则发布 SPEECH_SAY 事件）
        self.queue = queue

        self._last_speech_time: Dict[str, float] = {}
        self._running = False

        # 等级排序
        self._level_order = {
            SpeechLevel.S: 0,
            SpeechLevel.A: 1,
            SpeechLevel.B: 2,
            SpeechLevel.C: 3,
            SpeechLevel.DROP: 4,
        }

    def start(self):
        """启动 Speech Judge"""
        self._running = True

        # 订阅所有游戏事件
        for event_type in self.EVENT_LEVELS:
            self.event_bus.subscribe(event_type, self._on_event)

        print("[SpeechJudge] 已启动")

    def stop(self):
        """停止"""
        self._running = False

    def _on_event(self, event: Event):
        """处理事件"""
        if not self._running:
            return

        # 特殊处理：击杀聚合
        if event.event_type == EventType.CHAMPION_KILL:
            aggregated = self.kill_aggregator.add(event)
            if aggregated:
                # 聚合后的多杀事件
                self._judge_and_speak(aggregated)
            return

        # 普通事件评判
        self._judge_and_speak(event)

    def _judge_and_speak(self, event: Event):
        """评判事件并决定是否播报"""
        level = self._get_event_level(event)

        # 等级过滤
        if not self._should_speak(level):
            return

        # 频率限制
        if not self._check_frequency(event):
            return

        # 生成播报文本
        text = self._generate_speech_text(event, level)
        if not text:
            return

        # 构造 SpeechItem
        priority = LEVEL_TO_PRIORITY.get(level, PRIORITY_B)
        category = self._get_category(event)
        ttl = self._get_ttl(level, category, event)
        event_id = self._get_event_id(event)

        item = SpeechItem(
            text=text,
            priority=priority,
            expire_at=time.time() + ttl,
            category=category,
            event_id=event_id,
            interruptible=True,
        )

        # 交给 Queue 或发布 SPEECH_SAY 事件
        if self.queue:
            self.queue.enqueue(item)
        else:
            priority_str = "high" if level in ("S", "A") else "normal"
            self.event_bus.publish(Event(
                event_type=EventType.SPEECH_SAY,
                data={
                    "text": text,
                    "level": level,
                    "priority": priority_str,
                    "source_event": event.event_type,
                },
                source="speech_judge"
            ))

    def _get_category(self, event: Event) -> str:
        """获取事件分类"""
        event_type = event.event_type

        if event_type in (EventType.BARON_KILLED, EventType.DRAGON_KILLED, EventType.HERALD_KILLED):
            return "objective"
        if event_type in (EventType.CHAMPION_KILL, EventType.MULTI_KILL, EventType.FIRST_BLOOD, EventType.ACE):
            return "kill"
        if event_type == EventType.ALARM_TRIGGERED:
            return "alarm"
        if event_type == EventType.PLAYER_SUMMONER_SPELL_USED:
            return "spell"

        return "general"

    def _get_ttl(self, level: str, category: str, event: Event) -> float:
        """获取 TTL"""
        # category 覆盖
        if category in CATEGORY_TTL:
            return CATEGORY_TTL[category]

        # alarm 使用自己的过期时间
        if category == "alarm":
            alarm = event.get("alarm", {})
            if alarm.get("expire_at"):
                return max(0, alarm["expire_at"] - time.time())

        # 默认按等级
        return DEFAULT_TTL.get(level, 4.0)

    def _get_event_id(self, event: Event) -> str:
        """生成事件唯一 ID（用于去重）"""
        event_type = event.event_type
        game_time = event.get("game_time", 0)

        # 优先使用原生 EventID
        native_id = event.get("event_id") or event.get("EventID")
        if native_id:
            return f"liveclient:{event_type}:{native_id}"

        # fallback：组合 ID（用 timestamp 保证唯一性）
        return f"calc:{event_type}:{int(game_time)}:{int(event.timestamp * 1000)}"

    def _get_event_level(self, event: Event) -> str:
        """获取事件等级"""
        # 基础等级
        level = self.EVENT_LEVELS.get(event.event_type, SpeechLevel.C)

        # 根据强度调整
        if self.intensity == "sparse":
            # 稀疏模式：B 级降为 C
            if level == SpeechLevel.B:
                level = SpeechLevel.C
        elif self.intensity == "detailed":
            # 详细模式：C 级升为 B
            if level == SpeechLevel.C:
                level = SpeechLevel.B

        # 特殊情况：当前玩家相关的事件升级
        if event.get("is_current_player") or event.get("is_current_player_kill"):
            if level == SpeechLevel.C:
                level = SpeechLevel.B

        # 敌方技能使用（用户主动记录的）
        if event.event_type == EventType.ALARM_TRIGGERED:
            alarm = event.get("alarm", {})
            if alarm.get("type") in ("flash", "spell"):
                level = SpeechLevel.B

        return level

    def _should_speak(self, level: str) -> bool:
        """判断是否应该播报"""
        if level == SpeechLevel.DROP:
            return False

        level_order = self._level_order.get(level, 3)
        min_order = self._level_order.get(self.min_level, 2)
        return level_order <= min_order

    def _check_frequency(self, event: Event) -> bool:
        """检查播报频率限制"""
        event_type = event.event_type
        now = time.time()

        # 同一类型事件的最小间隔
        last_time = self._last_speech_time.get(event_type, 0)
        if now - last_time < self.min_interval:
            return False

        self._last_speech_time[event_type] = now
        return True

    def _generate_speech_text(self, event: Event, level: str) -> str:
        """生成播报文本"""
        event_type = event.event_type
        data = event.data

        # 中立资源
        if event_type == EventType.BARON_KILLED:
            killer = data.get("killer_name", "")
            return f"男爵被{killer}击杀了。"

        if event_type == EventType.DRAGON_KILLED:
            dragon_type = data.get("dragon_type", "")
            killer = data.get("killer_name", "")
            type_names = {
                "fire": "炼狱", "water": "海洋", "earth": "山脉",
                "air": "云端", "hextech": "海克斯", "chemtech": "炼金",
                "elder": "远古",
            }
            dt = type_names.get(dragon_type, dragon_type)
            return f"{dt}龙被{killer}击杀了。"

        if event_type == EventType.HERALD_KILLED:
            killer = data.get("killer_name", "")
            return f"潮虫先锋被{killer}击杀了。"

        # 击杀
        if event_type == EventType.CHAMPION_KILL:
            killer = data.get("killer_name", "")
            victim = data.get("victim_name", "")
            if data.get("is_current_player_kill"):
                return f"你击杀了{victim}。"
            elif data.get("is_current_player_death"):
                return f"你被{killer}击杀了。"
            return f"{killer}击杀了{victim}。"

        # 多杀
        if event_type == EventType.MULTI_KILL:
            count = data.get("count", 2)
            kill_names = {2: "双杀", 3: "三杀", 4: "四杀", 5: "五杀"}
            return f"{kill_names.get(count, f'{count}杀')}！"

        # 一血
        if event_type == EventType.FIRST_BLOOD:
            return "一血！"

        # 团灭
        if event_type == EventType.ACE:
            return "团灭！"

        # 防御塔
        if event_type == EventType.TOWER_DESTROYED:
            tower = data.get("tower_name", "")
            return f"防御塔被摧毁。"

        # 提醒触发
        if event_type == EventType.ALARM_TRIGGERED:
            alarm = data.get("alarm", {})
            title = alarm.get("title", "提醒")
            remaining = data.get("remaining", 0)
            if remaining > 0:
                return f"{title}还有{int(remaining)}秒。"
            return f"{title}好了。"

        # 召唤师技能使用（敌方）
        if event_type == EventType.PLAYER_SUMMONER_SPELL_USED:
            if data.get("is_enemy"):
                champion = data.get("champion_name", "")
                spell = data.get("spell_name", "")
                return f"对面{champion}{spell}用了。"

        return ""

    def set_intensity(self, intensity: str):
        """设置播报强度"""
        if intensity in ("sparse", "normal", "detailed"):
            self.intensity = intensity

    def set_min_level(self, level: str):
        """设置最低播报等级"""
        if level in (SpeechLevel.S, SpeechLevel.A, SpeechLevel.B, SpeechLevel.C):
            self.min_level = level
