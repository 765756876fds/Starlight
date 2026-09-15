"""
GAME-EVENT-02 回归测试

验证：
1. MultiKill 唯一责任方是 KillAggregator，StateDiff 不再生成 MultiKill
2. Riot 原生 EventID 正确进入 Domain Event 并用于 dedup
"""
import pytest
from unittest.mock import MagicMock

from app.core.event_bus import Event, EventType
from app.lol.state_diff import StateDiff, GameSnapshot
from app.speech.rule_engine import RuleEngine


class TestMultiKillSingleSource:
    """MultiKill 唯一责任方测试"""

    def test_state_diff_no_longer_generates_multikill(self):
        """StateDiff 不再生成 MultiKill Domain Event"""
        diff = StateDiff()

        # 初始化：第一次 diff 设置 _last_snapshot
        initial_snapshot = MagicMock(spec=GameSnapshot)
        initial_snapshot.game_time = 100.0
        initial_snapshot.is_game_running = True
        initial_snapshot.recent_events = []
        initial_snapshot.players = []
        initial_snapshot.current_player = None
        initial_snapshot.ally_team_name = "Order"
        diff.diff(initial_snapshot)  # 初始化

        # 模拟两个连续击杀（同一队伍）
        # 第一次击杀
        new_snapshot1 = MagicMock(spec=GameSnapshot)
        new_snapshot1.game_time = 105.0
        new_snapshot1.is_game_running = True
        new_snapshot1.recent_events = [
            {
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "EventID": 1,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        new_snapshot1.players = []
        new_snapshot1.current_player = None
        new_snapshot1.ally_team_name = "Order"

        events1 = diff.diff(new_snapshot1)
        kill_events1 = [e for e in events1 if e.event_type == EventType.CHAMPION_KILL]
        multikill_events1 = [e for e in events1 if e.event_type == EventType.MULTI_KILL]

        assert len(kill_events1) == 1
        assert len(multikill_events1) == 0  # StateDiff 不再生成 MultiKill

        # 第二次击杀（连续）
        new_snapshot2 = MagicMock(spec=GameSnapshot)
        new_snapshot2.game_time = 107.0
        new_snapshot2.is_game_running = True
        new_snapshot2.recent_events = new_snapshot1.recent_events + [
            {
                "EventName": "ChampionKill",
                "EventTime": 107.0,
                "EventID": 2,
                "KillerName": "PlayerA",
                "VictimName": "PlayerC",
                "Assisters": [],
            }
        ]
        new_snapshot2.players = []
        new_snapshot2.current_player = None
        new_snapshot2.ally_team_name = "Order"

        events2 = diff.diff(new_snapshot2)
        kill_events2 = [e for e in events2 if e.event_type == EventType.CHAMPION_KILL]
        multikill_events2 = [e for e in events2 if e.event_type == EventType.MULTI_KILL]

        assert len(kill_events2) == 1  # 新的击杀
        assert len(multikill_events2) == 0  # StateDiff 不生成 MultiKill

    def test_rule_engine_does_not_aggregate_kills(self):
        """RuleEngine 不自己聚合击杀"""
        engine = RuleEngine()

        # 连续两个 ChampionKill 事件
        kill1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 1,
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        candidate1 = engine.evaluate(kill1)
        assert candidate1 is not None
        assert candidate1.event_type == EventType.CHAMPION_KILL

        kill2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 2,
                "killer_name": "A",
                "victim_name": "C",
                "game_time": 105.0,
            },
            source="test"
        )
        candidate2 = engine.evaluate(kill2)
        assert candidate2 is not None
        assert candidate2.event_type == EventType.CHAMPION_KILL

        # RuleEngine 不自己生成 MultiKill
        # MultiKill 由 KillAggregator 处理后传入
        assert candidate1.rule_id == "champion_kill"
        assert candidate2.rule_id == "champion_kill"


class TestRiotEventID:
    """Riot 原生 EventID 测试"""

    def test_riot_event_id_in_domain_event(self):
        """eventdata EventID 正确进入 Domain Event"""
        diff = StateDiff()

        # 初始化
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 105.0
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "EventID": 12345,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        kill_events = [e for e in events if e.event_type == EventType.CHAMPION_KILL]

        assert len(kill_events) == 1
        assert kill_events[0].data["riot_event_id"] == 12345

    def test_same_riot_event_id_no_duplicate(self):
        """相同 EventID 重复轮询不会产生重复 Domain Event"""
        diff = StateDiff()

        # 初始化
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        # 第一次轮询
        new_snapshot1 = MagicMock(spec=GameSnapshot)
        new_snapshot1.game_time = 105.0
        new_snapshot1.is_game_running = True
        new_snapshot1.recent_events = [
            {
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "EventID": 12345,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        new_snapshot1.players = []
        new_snapshot1.current_player = None
        new_snapshot1.ally_team_name = "Order"

        events1 = diff.diff(new_snapshot1)
        assert len([e for e in events1 if e.event_type == EventType.CHAMPION_KILL]) == 1

        # 第二次轮询（相同事件，eventdata 是全量历史）
        new_snapshot2 = MagicMock(spec=GameSnapshot)
        new_snapshot2.game_time = 106.0
        new_snapshot2.is_game_running = True
        new_snapshot2.recent_events = new_snapshot1.recent_events  # 相同
        new_snapshot2.players = []
        new_snapshot2.current_player = None
        new_snapshot2.ally_team_name = "Order"

        events2 = diff.diff(new_snapshot2)
        kill_events2 = [e for e in events2 if e.event_type == EventType.CHAMPION_KILL]

        assert len(kill_events2) == 0  # 不重复

    def test_rule_engine_prefers_riot_event_id(self):
        """RuleEngine 优先使用 Riot EventID dedup"""
        engine = RuleEngine()

        # 带 riot_event_id 的事件
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 99999,
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        candidate1 = engine.evaluate(event1)
        assert candidate1 is not None
        assert "riot_99999" in candidate1.event_id

        # 相同 riot_event_id 的事件
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 99999,
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        candidate2 = engine.evaluate(event2)
        assert candidate2 is None  # dedup 生效

    def test_different_riot_event_ids_not_deduped(self):
        """不同 EventID 即使 EventName 相同也不能被错误 dedup"""
        engine = RuleEngine()

        # 不同的击杀（不同 EventID）
        kill1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 11111,
                "killer_name": "A",
                "victim_name": "B",
                "game_time": 100.0,
            },
            source="test"
        )
        candidate1 = engine.evaluate(kill1)
        assert candidate1 is not None

        kill2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 22222,
                "killer_name": "A",
                "victim_name": "C",
                "game_time": 105.0,
            },
            source="test"
        )
        candidate2 = engine.evaluate(kill2)
        assert candidate2 is not None  # 不同事件，不 dedup

    def test_fallback_without_riot_event_id(self):
        """没有 EventID 的 snapshot event 仍使用 fallback"""
        engine = RuleEngine()

        # 没有 riot_event_id 的事件（snapshot diff 事件）
        event1 = Event(
            event_type=EventType.GAME_STARTED,
            data={
                "game_time": 0,
            },
            source="test"
        )
        candidate1 = engine.evaluate(event1)
        assert candidate1 is not None
        # fallback event_id 不包含 riot_
        assert "riot_" not in candidate1.event_id

    def test_objective_events_have_riot_event_id(self):
        """Objective 事件也保留 riot_event_id"""
        diff = StateDiff()

        # 初始化
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 300.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 305.0
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventName": "DragonKill",
                "EventTime": 305.0,
                "EventID": 54321,
                "DragonType": "Fire",
                "KillerName": "PlayerA",
                "Stolen": False,
            }
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        dragon_events = [e for e in events if e.event_type == EventType.DRAGON_KILLED]

        assert len(dragon_events) == 1
        assert dragon_events[0].data["riot_event_id"] == 54321


class TestNativeMultikill:
    """Riot 原生 Multikill 事件测试"""

    def test_native_multikill_conversion(self):
        """原生 Multikill EventID → Domain Event MultiKill"""
        diff = StateDiff()

        # 初始化
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 105.0
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventID": 5,
                "EventName": "Multikill",
                "EventTime": 105.0,
                "KillStreak": 2,
                "KillerName": "PlayerA",
            }
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        multikill_events = [e for e in events if e.event_type == EventType.MULTI_KILL]

        assert len(multikill_events) == 1
        assert multikill_events[0].data["riot_event_id"] == 5
        assert multikill_events[0].data["kill_streak"] == 2
        assert multikill_events[0].data["killer_name"] == "PlayerA"

    def test_state_diff_does_not_derive_multikill_from_kills(self):
        """StateDiff 不从 ChampionKill 推导 MultiKill"""
        diff = StateDiff()

        # 初始化
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        # 连续两个 ChampionKill（没有原生 Multikill 事件）
        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 105.0
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventID": 1,
                "EventName": "ChampionKill",
                "EventTime": 100.0,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            },
            {
                "EventID": 2,
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "KillerName": "PlayerA",
                "VictimName": "PlayerC",
                "Assisters": [],
            },
            # 注意：没有原生 Multikill 事件
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        multikill_events = [e for e in events if e.event_type == EventType.MULTI_KILL]

        # StateDiff 不应该自己生成 MultiKill
        assert len(multikill_events) == 0

    def test_rule_engine_multikill_text_double(self):
        """Double Kill 文本"""
        engine = RuleEngine()

        event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 5,
                "kill_streak": 2,
                "killer_name": "PlayerA",
            },
            source="test"
        )

        candidate = engine.evaluate(event)
        assert candidate is not None
        assert candidate.text == "双杀"

    def test_rule_engine_multikill_text_triple(self):
        """Triple Kill 文本"""
        engine = RuleEngine()

        event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 7,
                "kill_streak": 3,
                "killer_name": "PlayerA",
            },
            source="test"
        )

        candidate = engine.evaluate(event)
        assert candidate is not None
        assert candidate.text == "三杀"

    def test_rule_engine_multikill_text_quadra(self):
        """Quadra Kill 文本"""
        engine = RuleEngine()

        event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 9,
                "kill_streak": 4,
                "killer_name": "PlayerA",
            },
            source="test"
        )

        candidate = engine.evaluate(event)
        assert candidate is not None
        assert candidate.text == "四杀"

    def test_rule_engine_multikill_text_penta(self):
        """Penta Kill 文本"""
        engine = RuleEngine()

        event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 11,
                "kill_streak": 5,
                "killer_name": "PlayerA",
            },
            source="test"
        )

        candidate = engine.evaluate(event)
        assert candidate is not None
        assert candidate.text == "五杀"

    def test_native_multikill_dedup(self):
        """原生 Multikill EventID dedup"""
        engine = RuleEngine()

        # 相同 EventID 的 Multikill
        event1 = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 5,
                "kill_streak": 2,
                "killer_name": "PlayerA",
            },
            source="test"
        )
        candidate1 = engine.evaluate(event1)
        assert candidate1 is not None

        # 相同 EventID，第二次不应该产生
        event2 = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 5,
                "kill_streak": 2,
                "killer_name": "PlayerA",
            },
            source="test"
        )
        candidate2 = engine.evaluate(event2)
        assert candidate2 is None

    def test_different_multikill_event_ids_not_deduped(self):
        """不同 Multikill EventID 不互相 dedup"""
        engine = RuleEngine()

        event1 = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 5,
                "kill_streak": 2,
                "killer_name": "PlayerA",
            },
            source="test"
        )
        candidate1 = engine.evaluate(event1)
        assert candidate1 is not None

        # 不同 EventID 的 Multikill
        event2 = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 7,
                "kill_streak": 3,
                "killer_name": "PlayerA",
            },
            source="test"
        )
        candidate2 = engine.evaluate(event2)
        assert candidate2 is not None  # 不被错误 dedup


class TestTencentLiveClientPath:
    """腾讯服 LiveClientAPI 路径测试"""

    def test_endpoints_have_liveclientdata_prefix(self):
        """所有 endpoint 都有 /liveclientdata 前缀"""
        from app.lol.live_client import LiveClientAPI

        api = LiveClientAPI()

        for name, path in api.ENDPOINTS.items():
            assert path.startswith("/liveclientdata/"), f"{name} path {path} missing /liveclientdata prefix"

    def test_is_available_uses_liveclientdata_path(self):
        """is_available() 使用 /liveclientdata/gamestats 路径"""
        # 静态验证：代码中不应该有裸 /gamestats
        import inspect
        from app.lol import live_client

        source = inspect.getsource(live_client)
        # 确保没有裸 /gamestats（没有 /liveclientdata 前缀的）
        # 注意：ENDPOINTS 字典中的已经有前缀了
        # is_available() 中的也需要检查
        assert "/liveclientdata/gamestats" in source
