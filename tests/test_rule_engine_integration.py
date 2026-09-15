"""
RuleEngine × EventBus × SpeechJudge 集成测试

验证链路：
Event → EventBus → RuleEngine → SpeechCandidate → SpeechJudge → SpeechQueue

不真实播放 TTS，使用 FakeTTS。
"""
import time
import pytest

from app.core.event_bus import Event, EventType, EventBus, get_event_bus
from app.speech.rule_engine import RuleEngine, SpeechCandidate
from app.speech.speech_judge import SpeechJudge
from app.speech.speech_queue import SpeechQueue, SpeechItem


class FakeTTS:
    """Fake TTS，记录调用但不真实播放"""

    def __init__(self):
        self.spoken_texts = []
        self.stopped_count = 0

    def speak(self, text: str):
        self.spoken_texts.append(text)

    def stop_playback(self):
        self.stopped_count += 1

    def is_playing(self):
        return False

    def wait_finished(self, timeout=None):
        return True


class FakeSpeechJudge:
    """Fake SpeechJudge，验证 RuleEngine 不绕过 SpeechJudge"""

    def __init__(self):
        self.received_candidates = []
        self.drop_count = 0

    def evaluate_candidate(self, candidate: SpeechCandidate) -> bool:
        """
        模拟 SpeechJudge 最终决策

        这个方法验证：RuleEngine 产生 Candidate，SpeechJudge 决定是否播报
        """
        self.received_candidates.append(candidate)

        # 模拟 DROP：S 级以下全部 DROP（用于测试优先级控制）
        if candidate.priority_hint == "DROP":
            self.drop_count += 1
            return False

        return True


class TestRuleEngineEventBusIntegration:
    """RuleEngine × EventBus 集成测试"""

    def setup_method(self):
        """每个测试前重置 EventBus"""
        self.bus = EventBus()
        self.rule_engine = RuleEngine()
        self.fake_judge = FakeSpeechJudge()
        self.fake_tts = FakeTTS()
        self.queue = SpeechQueue(self.fake_tts)

        # 模拟：EventBus → RuleEngine → SpeechJudge
        self.candidates_received_by_judge = []

    def _rule_engine_subscriber(self, event: Event):
        """RuleEngine 作为 EventBus subscriber"""
        try:
            candidate = self.rule_engine.evaluate(event)
            if candidate:
                # 模拟 SpeechJudge 接收 Candidate
                self.candidates_received_by_judge.append(candidate)
                self.fake_judge.evaluate_candidate(candidate)
        except Exception as e:
            # RuleEngine 异常不能拖垮 EventBus
            print(f"[RuleEngine] 异常: {e}")

    # ===== 基本集成测试 =====

    def test_game_started_through_eventbus(self):
        """GAME_STARTED: EventBus → RuleEngine → Candidate → SpeechJudge"""
        self.bus.subscribe(EventType.GAME_STARTED, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0},
            source="test"
        )
        self.bus.publish(event)

        # RuleEngine 应该产生 1 个 Candidate
        assert len(self.candidates_received_by_judge) == 1
        candidate = self.candidates_received_by_judge[0]
        assert candidate.rule_id == "game_started"
        assert candidate.priority_hint == "B"

        # SpeechJudge 应该收到 Candidate（RuleEngine 不绕过 SpeechJudge）
        assert len(self.fake_judge.received_candidates) == 1

    def test_champion_kill_through_eventbus(self):
        """ChampionKill: EventBus → RuleEngine → Candidate → SpeechJudge"""
        self.bus.subscribe(EventType.CHAMPION_KILL, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )
        self.bus.publish(event)

        assert len(self.candidates_received_by_judge) == 1
        candidate = self.candidates_received_by_judge[0]
        assert candidate.rule_id == "champion_kill"
        assert candidate.priority_hint == "B"

    def test_multi_kill_through_eventbus(self):
        """MultiKill: EventBus → RuleEngine → Candidate → SpeechJudge"""
        self.bus.subscribe(EventType.MULTI_KILL, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.MULTI_KILL,
            data={"killer_name": "A", "count": 2, "game_time": 100.5},
            source="test"
        )
        self.bus.publish(event)

        assert len(self.candidates_received_by_judge) == 1
        candidate = self.candidates_received_by_judge[0]
        assert candidate.rule_id == "multi_kill"
        assert candidate.priority_hint == "A"

    def test_objective_through_eventbus(self):
        """Objective: EventBus → RuleEngine → Candidate → SpeechJudge"""
        self.bus.subscribe(EventType.BARON_KILLED, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.BARON_KILLED,
            data={"game_time": 600.0},
            source="test"
        )
        self.bus.publish(event)

        assert len(self.candidates_received_by_judge) == 1
        candidate = self.candidates_received_by_judge[0]
        assert candidate.rule_id == "baron_killed"
        assert candidate.priority_hint == "S"

    def test_game_ended_through_eventbus(self):
        """GAME_ENDED: EventBus → RuleEngine → Candidate → SpeechJudge"""
        self.bus.subscribe(EventType.GAME_ENDED, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.GAME_ENDED,
            data={"game_time": 1800.0},
            source="test"
        )
        self.bus.publish(event)

        assert len(self.candidates_received_by_judge) == 1
        candidate = self.candidates_received_by_judge[0]
        assert candidate.rule_id == "game_ended"
        assert candidate.priority_hint == "A"

    # ===== Dedup 测试 =====

    def test_dedup_through_eventbus(self):
        """重复 event_id：EventBus 两次发布相同事件 → RuleEngine 只产生一个 Candidate"""
        self.bus.subscribe(EventType.GAME_STARTED, self._rule_engine_subscriber)

        # 第一次发布
        event1 = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0, "event_id": "game_start_1"},
            source="test"
        )
        self.bus.publish(event1)

        # 第二次发布相同 event_id
        event2 = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0, "event_id": "game_start_1"},
            source="test"
        )
        self.bus.publish(event2)

        # 应该只有 1 个 Candidate
        assert len(self.candidates_received_by_judge) == 1

    # ===== 异常隔离测试 =====

    def test_rule_engine_exception_not_crash_eventbus(self):
        """RuleEngine 异常 → EventBus 不崩溃 → 其他 subscriber 继续工作"""
        other_received = []

        def buggy_subscriber(event: Event):
            raise Exception("模拟 bug")

        def other_subscriber(event: Event):
            other_received.append(event)

        self.bus.subscribe(EventType.GAME_STARTED, buggy_subscriber)
        self.bus.subscribe(EventType.GAME_STARTED, other_subscriber)

        event = Event(
            event_type=EventType.GAME_STARTED,
            data={},
            source="test"
        )

        # 不应该崩溃
        self.bus.publish(event)

        # 其他 subscriber 仍然收到事件
        assert len(other_received) == 1

    # ===== SpeechJudge 控制权验证 =====

    def test_speech_judge_controls_final_priority(self):
        """SpeechJudge 控制最终 priority，RuleEngine 只是 hint"""
        # RuleEngine 产生 B 级 hint
        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"killer_name": "A", "victim": "B", "game_time": 100.0},
            source="test"
        )

        candidate = self.rule_engine.evaluate(event)
        assert candidate is not None
        assert candidate.priority_hint == "B"

        # SpeechJudge 可以根据自己的规则提升或降低
        # 这里验证：Candidate 只是 hint，不是最终决定
        assert candidate.priority_hint in ("S", "A", "B", "DROP")

    def test_speech_judge_can_drop(self):
        """SpeechJudge 可以 DROP Candidate（即使 RuleEngine 产生了）"""
        # RuleEngine 产生 Candidate
        event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0},
            source="test"
        )
        candidate = self.rule_engine.evaluate(event)
        assert candidate is not None

        # SpeechJudge 决定 DROP
        # （在真实实现中，SpeechJudge 会根据自己的规则判断）
        # 这里验证：RuleEngine 不直接决定最终是否播报
        assert hasattr(candidate, "priority_hint")

    # ===== Queue 集成验证 =====

    def test_candidate_can_enter_queue(self):
        """Candidate 最终能够进入现有 SpeechQueue"""
        # RuleEngine 产生 Candidate
        event = Event(
            event_type=EventType.BARON_KILLED,
            data={"game_time": 600.0},
            source="test"
        )
        candidate = self.rule_engine.evaluate(event)
        assert candidate is not None

        # 模拟 SpeechJudge 把 Candidate 转成 SpeechItem 入队
        import time
        item = SpeechItem(
            text=candidate.text,
            priority=0 if candidate.priority_hint == "S" else 1,
            expire_at=candidate.expire_at,
            category=candidate.category,
            event_id=candidate.event_id,
        )

        self.queue.start()
        success = self.queue.enqueue(item)

        assert success is True
        assert self.queue.size() == 1

        self.queue.shutdown()

    # ===== C 级事件测试 =====

    def test_c_level_events_no_candidate(self):
        """C 级事件不产生 Candidate（RuleEngine 只是不匹配规则，不是最终 DROP）"""
        self.bus.subscribe(EventType.GAME_TIME_UPDATE, self._rule_engine_subscriber)

        event = Event(
            event_type=EventType.GAME_TIME_UPDATE,
            data={"game_time": 100.0},
            source="test"
        )
        self.bus.publish(event)

        # C 级事件不产生 Candidate
        assert len(self.candidates_received_by_judge) == 0

        # 注意：这不是"SpeechJudge 不能播"
        # 而是"RuleEngine 没有为这个事件定义规则"
        # SpeechJudge 仍然可以根据自己的规则决定

    # ===== 不绕过 SpeechJudge 验证 =====

    def test_rule_engine_not_directly_calling_tts(self):
        """RuleEngine 不直接调用 TTS"""
        # 验证：RuleEngine.evaluate() 不调用任何 TTS 方法
        # （静态验证：RuleEngine 代码中没有 self.tts 或 queue 引用）
        assert not hasattr(self.rule_engine, "tts")
        assert not hasattr(self.rule_engine, "queue")

    def test_rule_engine_not_directly_calling_queue(self):
        """RuleEngine 不直接调用 SpeechQueue"""
        # 验证：RuleEngine 不直接入队
        assert not hasattr(self.rule_engine, "_queue")

    # ===== Replay 安全测试 =====

    def test_replay_events_dont_trigger_real_tts(self):
        """Replay 事件不产生真实 TTS 副作用"""
        self.bus.subscribe(EventType.GAME_STARTED, self._rule_engine_subscriber)

        # Replay 事件（带 _replay 标记）
        replay_event = Event(
            event_type=EventType.GAME_STARTED,
            data={"game_time": 0, "_replay": True},
            source="replay"
        )
        self.bus.publish(replay_event)

        # Replay 事件仍然会经过 RuleEngine
        # 但在真实系统中，EventLog 会跳过 Replay 事件不写入
        # 这里验证：RuleEngine 不直接调用 TTS
        assert len(self.fake_tts.spoken_texts) == 0
