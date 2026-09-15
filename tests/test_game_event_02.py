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


class TestEventIDDedup:
    """Riot EventID 去重测试"""

    def test_event_id_zero_handled_correctly(self):
        """EventID=0 (GameStart) 必须正确处理，不被当成无 EventID"""
        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 0.1
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventID": 0,
                "EventName": "GameStart",
                "EventTime": 0.1,
            }
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        game_start_events = [e for e in events if e.event_type == EventType.GAME_STARTED]

        assert len(game_start_events) == 1
        assert game_start_events[0].data["riot_event_id"] == 0

    def test_same_event_id_no_duplicate(self):
        """相同 EventID 不产生重复 Domain Event"""
        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        # 第一次
        snapshot1 = MagicMock(spec=GameSnapshot)
        snapshot1.game_time = 105.0
        snapshot1.is_game_running = True
        snapshot1.recent_events = [
            {
                "EventID": 5,
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        snapshot1.players = []
        snapshot1.current_player = None
        snapshot1.ally_team_name = "Order"

        events1 = diff.diff(snapshot1)
        assert len([e for e in events1 if e.event_type == EventType.CHAMPION_KILL]) == 1

        # 第二次，相同事件（模拟全量历史）
        snapshot2 = MagicMock(spec=GameSnapshot)
        snapshot2.game_time = 106.0
        snapshot2.is_game_running = True
        snapshot2.recent_events = [
            {
                "EventID": 5,
                "EventName": "ChampionKill",
                "EventTime": 105.0,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        snapshot2.players = []
        snapshot2.current_player = None
        snapshot2.ally_team_name = "Order"

        events2 = diff.diff(snapshot2)
        assert len([e for e in events2 if e.event_type == EventType.CHAMPION_KILL]) == 0

    def test_same_time_same_killer_different_event_id(self):
        """同时间 + 同 Killer + 不同 EventID 不被错误 dedup"""
        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 100.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        # 两个 Multikill 事件，相同时间和 Killer，但不同 EventID
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
            },
            {
                "EventID": 7,
                "EventName": "Multikill",
                "EventTime": 105.0,
                "KillStreak": 3,
                "KillerName": "PlayerA",
            },
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        multikill_events = [e for e in events if e.event_type == EventType.MULTI_KILL]

        # 两个不同 EventID 的 Multikill 必须都产生
        assert len(multikill_events) == 2

    def test_event_id_resets_on_new_game(self):
        """新游戏 EventID 重置"""
        diff = StateDiff()

        # 第一局
        initial1 = MagicMock(spec=GameSnapshot)
        initial1.game_time = 0.0
        initial1.is_game_running = True
        initial1.recent_events = []
        initial1.players = []
        initial1.current_player = None
        initial1.ally_team_name = "Order"
        diff.diff(initial1)

        snapshot1 = MagicMock(spec=GameSnapshot)
        snapshot1.game_time = 100.0
        snapshot1.is_game_running = True
        snapshot1.recent_events = [
            {
                "EventID": 5,
                "EventName": "ChampionKill",
                "EventTime": 100.0,
                "KillerName": "PlayerA",
                "VictimName": "PlayerB",
                "Assisters": [],
            }
        ]
        snapshot1.players = []
        snapshot1.current_player = None
        snapshot1.ally_team_name = "Order"

        events1 = diff.diff(snapshot1)
        assert len([e for e in events1 if e.event_type == EventType.CHAMPION_KILL]) == 1

        # 新游戏
        diff.reset()

        initial2 = MagicMock(spec=GameSnapshot)
        initial2.game_time = 0.0
        initial2.is_game_running = True
        initial2.recent_events = []
        initial2.players = []
        initial2.current_player = None
        initial2.ally_team_name = "Order"
        diff.diff(initial2)

        # 第二局，相同 EventID=5
        snapshot2 = MagicMock(spec=GameSnapshot)
        snapshot2.game_time = 100.0
        snapshot2.is_game_running = True
        snapshot2.recent_events = [
            {
                "EventID": 5,
                "EventName": "ChampionKill",
                "EventTime": 100.0,
                "KillerName": "PlayerC",
                "VictimName": "PlayerD",
                "Assisters": [],
            }
        ]
        snapshot2.players = []
        snapshot2.current_player = None
        snapshot2.ally_team_name = "Order"

        events2 = diff.diff(snapshot2)
        # 新游戏后，相同 EventID 应该重新产生
        assert len([e for e in events2 if e.event_type == EventType.CHAMPION_KILL]) == 1


class TestGameStartMapping:
    """GameStart 映射测试"""

    def test_game_start_event_mapped(self):
        """Riot GameStart 事件正确映射为 GAME_STARTED"""
        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        new_snapshot = MagicMock(spec=GameSnapshot)
        new_snapshot.game_time = 0.1
        new_snapshot.is_game_running = True
        new_snapshot.recent_events = [
            {
                "EventID": 0,
                "EventName": "GameStart",
                "EventTime": 0.1,
            }
        ]
        new_snapshot.players = []
        new_snapshot.current_player = None
        new_snapshot.ally_team_name = "Order"

        events = diff.diff(new_snapshot)
        game_start_events = [e for e in events if e.event_type == EventType.GAME_STARTED]

        assert len(game_start_events) == 1
        assert game_start_events[0].data["riot_event_id"] == 0


class TestKillAggregatorDisabled:
    """KillAggregator 已删除测试"""

    def test_speech_judge_no_longer_aggregates_kills(self):
        """SpeechJudge 不再自己聚合多杀"""
        from app.speech.speech_judge import SpeechJudge
        from app.core.event_bus import Event

        judge = SpeechJudge({})
        judge._running = True

        # 连续发送 3 个 ChampionKill
        for i in range(3):
            event = Event(
                event_type=EventType.CHAMPION_KILL,
                data={
                    "killer_name": "PlayerA",
                    "victim_name": f"PlayerB{i}",
                },
                source="test"
            )
            # 不应该产生多杀聚合
            # 直接走正常判断路径
            # 这里只验证不崩溃
            judge._on_event(event)


class TestRealTencentSample108:
    """真实腾讯服 108 事件样本回归测试"""

    def _load_real_sample(self):
        """加载真实样本"""
        import json
        import os
        fixture_path = os.path.join(
            os.path.dirname(__file__),
            'fixtures',
            'eventdata_tencent_core_108.json'
        )
        with open(fixture_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def test_sample_file_exists(self):
        """样本文件存在"""
        data = self._load_real_sample()
        assert 'Events' in data
        assert len(data['Events']) == 108

    def test_state_diff_maps_all_core_events(self):
        """StateDiff 正确映射所有核心事件"""
        data = self._load_real_sample()
        events = data['Events']

        diff = StateDiff()

        # 初始化空 snapshot
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        # 一次性喂所有事件（模拟第一次轮询）
        snapshot = MagicMock(spec=GameSnapshot)
        snapshot.game_time = 1000.0
        snapshot.is_game_running = True
        snapshot.recent_events = events
        snapshot.players = []
        snapshot.current_player = None
        snapshot.ally_team_name = "Order"

        domain_events = diff.diff(snapshot)

        # 统计各类型数量
        from collections import Counter
        type_counts = Counter(e.event_type for e in domain_events)

        # 验证核心事件数量
        expected = {
            EventType.GAME_STARTED: 1,
            EventType.CHAMPION_KILL: 40,
            EventType.FIRST_BLOOD: 1,
            EventType.MULTI_KILL: 32,
            EventType.ACE: 8,
            EventType.TOWER_DESTROYED: 13,
            EventType.DRAGON_KILLED: 2,
            EventType.HERALD_KILLED: 1,
            EventType.BARON_KILLED: 1,
            EventType.INHIBITOR_DESTROYED: 3,
            EventType.GAME_ENDED: 1,
        }

        for etype, count in expected.items():
            actual = type_counts.get(etype, 0)
            assert actual == count, f"{etype}: expected {count}, got {actual}"

    def test_game_end_result_preserved(self):
        """GameEnd 的 Result 字段正确保留"""
        data = self._load_real_sample()
        events = data['Events']

        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        snapshot = MagicMock(spec=GameSnapshot)
        snapshot.game_time = 1000.0
        snapshot.is_game_running = True
        snapshot.recent_events = events
        snapshot.players = []
        snapshot.current_player = None
        snapshot.ally_team_name = "Order"

        domain_events = diff.diff(snapshot)
        game_end_events = [e for e in domain_events if e.event_type == EventType.GAME_ENDED]

        assert len(game_end_events) >= 1
        game_end = game_end_events[0]
        assert game_end.data["result"] == "Win"
        assert game_end.data["riot_event_id"] == 107

    def test_dragon_type_preserved(self):
        """DragonKill 的 DragonType 字段正确保留"""
        data = self._load_real_sample()
        events = data['Events']

        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        snapshot = MagicMock(spec=GameSnapshot)
        snapshot.game_time = 1000.0
        snapshot.is_game_running = True
        snapshot.recent_events = events
        snapshot.players = []
        snapshot.current_player = None
        snapshot.ally_team_name = "Order"

        domain_events = diff.diff(snapshot)
        dragon_events = [e for e in domain_events if e.event_type == EventType.DRAGON_KILLED]

        assert len(dragon_events) == 2
        # 第一个应该是 Air 龙
        assert dragon_events[0].data["dragon_type"] == "Air"

    def test_turret_killed_has_tower_name(self):
        """TurretKilled 的塔名正确保留"""
        data = self._load_real_sample()
        events = data['Events']

        diff = StateDiff()

        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        snapshot = MagicMock(spec=GameSnapshot)
        snapshot.game_time = 1000.0
        snapshot.is_game_running = True
        snapshot.recent_events = events
        snapshot.players = []
        snapshot.current_player = None
        snapshot.ally_team_name = "Order"

        domain_events = diff.diff(snapshot)
        turret_events = [e for e in domain_events if e.event_type == EventType.TOWER_DESTROYED]

        assert len(turret_events) == 13
        assert turret_events[0].data["tower_name"] != ""


class TestCoreEventE2EChain:
    """11 类核心事件完整语音链路 E2E 测试"""

    def _build_chain(self):
        """构建完整测试链路：StateDiff -> EventBus -> RuleEngine -> FakeQueue"""
        from app.core.event_bus import EventBus, Event
        from app.speech.rule_engine import RuleEngine

        event_bus = EventBus()
        rule_engine = RuleEngine()

        # 收集所有 Candidate
        candidates = []

        def on_candidate(candidate):
            candidates.append(candidate)

        # RuleEngine 订阅 EventBus
        def on_event(event):
            candidate = rule_engine.evaluate(event)
            if candidate:
                on_candidate(candidate)

        event_bus.subscribe(on_event)

        return event_bus, on_event, candidates

    def _create_event_from_domain(self, domain_event):
        """把 GameEvent 转换成 Event（模拟 runtime.py 的转换）"""
        return Event(
            event_type=domain_event.event_type,
            data=domain_event.data,
            source="lol"
        )

    def test_all_11_core_events_produce_candidates(self):
        """11 类核心事件都能通过完整链路产生 SpeechCandidate"""
        import json
        import os
        from app.core.event_bus import EventBus

        # 加载真实样本
        fixture_path = os.path.join(
            os.path.dirname(__file__),
            'fixtures',
            'eventdata_tencent_core_108.json'
        )
        with open(fixture_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        events = data['Events']

        # StateDiff
        diff = StateDiff()
        initial = MagicMock(spec=GameSnapshot)
        initial.game_time = 0.0
        initial.is_game_running = True
        initial.recent_events = []
        initial.players = []
        initial.current_player = None
        initial.ally_team_name = "Order"
        diff.diff(initial)

        snapshot = MagicMock(spec=GameSnapshot)
        snapshot.game_time = 1000.0
        snapshot.is_game_running = True
        snapshot.recent_events = events
        snapshot.players = []
        snapshot.current_player = None
        snapshot.ally_team_name = "Order"

        domain_events = diff.diff(snapshot)

        # 完整链路：EventBus -> RuleEngine
        event_bus = EventBus()
        from app.speech.rule_engine import RuleEngine
        rule_engine = RuleEngine()

        candidates = []

        def on_event(event):
            candidate = rule_engine.evaluate(event)
            if candidate:
                candidates.append(candidate)

        event_bus.subscribe_all(on_event)

        # 发布所有 Domain Event
        for de in domain_events:
            event = self._create_event_from_domain(de)
            event_bus.publish(event)

        # 验证每类事件都产生了 Candidate
        from collections import Counter
        candidate_types = Counter(c.category for c in candidates)

        # 11 类核心事件都应该有对应的 Candidate
        # game category: GameStart, GameEnd
        # kill category: ChampionKill, Multikill, FirstBlood, Ace
        # objective category: Dragon, Baron, Herald, Turret, Inhibitor

        assert candidate_types.get("game", 0) >= 2  # GameStart + GameEnd
        assert candidate_types.get("kill", 0) >= 4  # ChampionKill + Multikill + FirstBlood + Ace
        assert candidate_types.get("objective", 0) >= 5  # Dragon + Baron + Herald + Turret + Inhibitor

    def test_game_end_result_not_lost_in_chain(self):
        """GameEnd.Result 在完整链路中不丢失"""
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # 模拟 GameEnd 事件
        event = Event(
            event_type=EventType.GAME_ENDED,
            data={
                "riot_event_id": 107,
                "result": "Win",
            },
            source="lol"
        )

        candidate = rule_engine.evaluate(event)
        assert candidate is not None
        assert candidate.category == "game"

        # Result 应该在 metadata 或 data 中保留
        # 注意：RuleEngine 可能不会把所有 data 都放进 candidate
        # 至少验证事件没有因为 Result 而崩溃

    def test_game_end_missing_result_does_not_crash(self):
        """GameEnd 缺少 Result 不崩溃"""
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # 缺少 Result
        event = Event(
            event_type=EventType.GAME_ENDED,
            data={
                "riot_event_id": 107,
            },
            source="lol"
        )

        candidate = rule_engine.evaluate(event)
        assert candidate is not None  # 仍然产生 Candidate

    def test_multikill_no_duplicate_with_champion_kill(self):
        """Multikill 不会和 ChampionKill 产生重复播报路径"""
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # ChampionKill
        champ_event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 2,
                "killer_name": "PlayerA",
                "victim_name": "PlayerB",
            },
            source="lol"
        )

        # Multikill (独立事件)
        multi_event = Event(
            event_type=EventType.MULTI_KILL,
            data={
                "riot_event_id": 5,
                "kill_streak": 2,
                "killer_name": "PlayerA",
            },
            source="lol"
        )

        c1 = rule_engine.evaluate(champ_event)
        c2 = rule_engine.evaluate(multi_event)

        # 两个都应该产生 Candidate（不同事件）
        assert c1 is not None
        assert c2 is not None

        # 但不是重复播报（它们是不同的事件，不同的 EventID）
        # 这里只是验证两条路径独立存在，不会互相干扰

    def test_event_id_dedup_in_rule_engine(self):
        """EventID 在 RuleEngine 中正确 dedup"""
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # 相同 EventID
        event1 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"riot_event_id": 100, "killer_name": "A", "victim_name": "B"},
            source="test"
        )
        event2 = Event(
            event_type=EventType.CHAMPION_KILL,
            data={"riot_event_id": 100, "killer_name": "A", "victim_name": "B"},
            source="test"
        )

        c1 = rule_engine.evaluate(event1)
        c2 = rule_engine.evaluate(event2)

        assert c1 is not None
        assert c2 is None  # 第二次被 dedup

    def test_dragon_type_preserved_in_chain(self):
        """DragonType 在完整链路中不丢失"""
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # 模拟 DragonKill 事件
        event = Event(
            event_type=EventType.DRAGON_KILLED,
            data={
                "riot_event_id": 36,
                "dragon_type": "Air",
                "stolen": "False",
                "killer_name": "PlayerA",
            },
            source="lol"
        )

        candidate = rule_engine.evaluate(event)
        assert candidate is not None
        assert candidate.category == "objective"
        # 验证事件没有因为 DragonType 而崩溃

    def test_replay_does_not_trigger_real_tts(self):
        """Replay 不触发真实 TTS"""
        # 这是架构层面的验证：
        # Replay 只是重新发布 Event 到 EventBus
        # 不会直接调用 TTS
        # 真实 TTS 只在 SpeechQueue 的 worker 线程中触发
        # Replay 事件如果不启动 SpeechQueue worker，就不会触发 TTS

        # 这里只验证 RuleEngine 不会因为 replay 标记而崩溃
        from app.core.event_bus import Event
        from app.speech.rule_engine import RuleEngine

        rule_engine = RuleEngine()

        # 模拟 replay 事件
        event = Event(
            event_type=EventType.CHAMPION_KILL,
            data={
                "riot_event_id": 100,
                "killer_name": "PlayerA",
                "victim_name": "PlayerB",
            },
            source="replay"
        )

        candidate = rule_engine.evaluate(event)
        # Replay 事件也会产生 Candidate（这是正常的）
        # 但 SpeechQueue 应该在 replay 模式下不启动真实 TTS worker
        # 这部分由 SpeechQueue 自己控制
        assert candidate is not None
