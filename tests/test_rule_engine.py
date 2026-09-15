"""
RuleEngine 单元测试
"""
import time
import pytest

from app.core.event_bus import Event, EventType
from app.speech.rule_engine import RuleEngine, SpeechCandidate


class TestRuleEngine:
    """RuleEngine 测试"""

    def setup_method(self):
        """每个测试前创建新的 RuleEngine"""
        self.engine = RuleEngine()

    # ===== 基本规则测试 =====

    def test_game_started_produces_candidate(self):
        """GAME_STARTED → 1 Candidate"""
        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "game_started"
        assert candidate.event_type == EventType.GAME_STARTED
        assert candidate.category == "game"
        assert candidate.priority_hint == "B"
        assert "游戏开始" in candidate.text

    def test_game_started_dedup(self):
        """GAME_STARTED 同 event_id 重复 → 0 Candidate"""
        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0, "event_id": "game_start_1"},
            source="test"
        )

        # 第一次
        candidate1 = self.engine.evaluate(event)
        assert candidate1 is not None

        # 第二次（相同 event_id）
        candidate2 = self.engine.evaluate(event)
        assert candidate2 is None  # dedup

    def test_game_ended_produces_candidate(self):
        """GAME_ENDED → 1 Candidate"""
        event = Event(
            event_type=EventType.GAME_ENDED,
            data={"game_time": 1800},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "game_ended"
        assert candidate.priority_hint == "A"

    def test_game_ended_dedup(self):
        """GAME_ENDED 重复 → 0 Candidate"""
        event = Event(
            event_type=EventType.GAME_ENDED,
            data={"game_time": 1800, "event_id": "game_end_1"},
            source="test"
        )

        self.engine.evaluate(event)
        candidate2 = self.engine.evaluate(event)

        assert candidate2 is None

    # ===== 击杀事件测试 =====

    def test_champion_kill_produces_candidate(self):
        """ChampionKill → 1 Candidate"""
        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "killer_name": "孤灯不慰失意人",
                "victim_name": "亚索",
                "game_time": 100.0,
            },
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "champion_kill"
        assert candidate.category == "kill"
        assert candidate.priority_hint == "B"

    def test_multi_kill_produces_candidate(self):
        """MultiKill → 1 Candidate"""
        event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "killer_name": "孤灯不慰失意人",
                "count": 3,
                "game_time": 120.0,
            },
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "multi_kill"
        assert candidate.priority_hint == "A"

    def test_multi_kill_not_double_aggregated(self):
        """MultiKill 不被 ChampionKill 再次重复聚合"""
        # 先发生 ChampionKill
        kill_event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )
        self.engine.evaluate(kill_event)

        # 然后发生 MultiKill（不同 event_id）
        multi_event = Event(
            event_type=EventType.MULTI_KILL,
            data={"killer_name": "A", "count": 2, "game_time": 100.5},
            source="test"
        )
        candidate = self.engine.evaluate(multi_event)

        # MultiKill 应该产生独立 candidate，而不是被当作普通 kill
        assert candidate is not None
        assert candidate.rule_id == "multi_kill"
        assert candidate.priority_hint == "A"

    # ===== Objective 事件测试 =====

    def test_dragon_killed_produces_candidate(self):
        """Dragon 有可靠 Event → Candidate"""
        event = Event(
            event_type=EventType.DRAGON_KILLED,
            data={"game_time": 300.0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "dragon_killed"
        assert candidate.category == "objective"
        assert candidate.priority_hint == "A"

    def test_baron_killed_produces_s_candidate(self):
        """Baron → S 级 Candidate"""
        event = Event(
            event_type=EventType.BARON_KILLED,
            data={"game_time": 600.0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert candidate.rule_id == "baron_killed"
        assert candidate.priority_hint == "S"

    def test_unknown_objective_no_candidate(self):
        """不支持/不可靠 Objective → 不产生 Candidate"""
        # 使用一个不在规则表中的事件类型
        event = Event(
            event_type="game.unknown_objective",
            data={"game_time": 100.0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is None

    # ===== Timer 测试 =====

    def test_game_time_update_no_candidate(self):
        """Timer 普通倒计时变化 → 不产生 Candidate"""
        event = Event(
            event_type=EventType.GAME_TIME_UPDATE,
            data={"game_time": 100.0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is None  # C 级

    # ===== Dedup fallback 测试 =====

    def test_dedup_fallback_without_event_id(self):
        """Event 缺失 event_id 时 fallback dedup"""
        # 相同 game_time + killer + victim = 相同 event_id
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )
        candidate1 = self.engine.evaluate(event1)
        assert candidate1 is not None

        # 相同的逻辑事件（无 event_id）
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )
        candidate2 = self.engine.evaluate(event2)
        assert candidate2 is None  # dedup 生效

    def test_different_kill_same_time_not_deduped(self):
        """不同击杀（不同 killer/victim）即使 game_time 相同也不 dedup"""
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )
        self.engine.evaluate(event1)

        # 同一时间但不同击杀
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "C", "victim": "D", "game_time": 100.0},
            source="test"
        )
        candidate2 = self.engine.evaluate(event2)

        assert candidate2 is not None  # 不同事件

    # ===== 异常处理测试 =====

    def test_malformed_event_no_crash(self):
        """malformed event → 不崩溃"""
        # 缺少 data 的事件
        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={},
            source="test"
        )

        # 不应该崩溃
        candidate = self.engine.evaluate(event)
        # 可能返回 candidate，也可能不返回，但不能崩溃
        assert candidate is None or isinstance(candidate, SpeechCandidate)

    def test_clear_dedup_state(self):
        """clear() 清除 dedup 状态"""
        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"event_id": "game_1"},
            source="test"
        )

        # 第一次
        self.engine.evaluate(event)
        assert self.engine.get_seen_count() == 1

        # 第二次（dedup）
        self.engine.evaluate(event)
        assert self.engine.get_seen_count() == 1

        # clear 后
        self.engine.clear()
        assert self.engine.get_seen_count() == 0

        # 再次 evaluate
        candidate = self.engine.evaluate(event)
        assert candidate is not None

    # ===== C 级事件测试 =====

    def test_c_level_events_no_candidate(self):
        """C 级事件不产生 candidate"""
        c_events = [
            EventType.PLAYER_LEVEL_UP,
            EventType.PLAYER_ITEM_CHANGED,
            EventType.GAME_TIME_UPDATE,
        ]

        for event_type in c_events:
            event = Event(
                event_type=event_type,
                data={},
                source="test"
            )
            candidate = self.engine.evaluate(event)
            assert candidate is None, f"{event_type} 应该 DROP"

    # ===== SpeechCandidate 结构测试 =====

    def test_candidate_has_required_fields(self):
        """Candidate 包含所有必要字段"""
        event = Event(
            event_type=EventType.BARON_KILLED,
            data={"game_time": 600.0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate is not None
        assert hasattr(candidate, "rule_id")
        assert hasattr(candidate, "event_id")
        assert hasattr(candidate, "event_type")
        assert hasattr(candidate, "category")
        assert hasattr(candidate, "text")
        assert hasattr(candidate, "priority_hint")
        assert hasattr(candidate, "created_at")
        assert hasattr(candidate, "expire_at")
        assert hasattr(candidate, "interruptible")
        assert hasattr(candidate, "metadata")

    def test_candidate_expire_at_in_future(self):
        """expire_at 在未来"""
        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0},
            source="test"
        )

        candidate = self.engine.evaluate(event)

        assert candidate.expire_at > time.time()
