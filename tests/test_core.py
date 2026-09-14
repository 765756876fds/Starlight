"""
Core 模块测试：EventType, EventBus, StateDiff, GameAlarm
"""
import time
import pytest

from app.core.event_bus import EventBus, Event, EventType
from app.lol.events import GameEventType, GameEvent


class TestEventType:
    """EventType 统一性测试"""

    def test_game_event_type_is_alias(self):
        """GameEventType 应该是 EventType 的别名"""
        assert GameEventType is EventType

    def test_game_started(self):
        assert EventType.GAME_STARTED == "game.started"

    def test_game_ended(self):
        assert EventType.GAME_ENDED == "game.ended"

    def test_game_time_update(self):
        assert EventType.GAME_TIME_UPDATE == "game.time_update"

    def test_player_level_up(self):
        assert EventType.PLAYER_LEVEL_UP == "game.player_level_up"

    def test_champion_kill(self):
        assert EventType.CHAMPION_KILL == "game.champion_kill"

    def test_tower_destroyed(self):
        assert EventType.TOWER_DESTROYED == "game.tower_destroyed"

    def test_dragon_killed(self):
        assert EventType.DRAGON_KILLED == "game.dragon_killed"

    def test_baron_killed(self):
        assert EventType.BARON_KILLED == "game.baron_killed"

    def test_item_changed(self):
        assert EventType.ITEM_CHANGED == "game.item_changed"

    def test_summoner_spell_used(self):
        assert EventType.PLAYER_SUMMONER_SPELL_USED == "game.player_summoner_spell_used"


class TestEventBus:
    """EventBus 测试"""

    def test_publish_and_subscribe(self, event_bus):
        received = []

        def handler(event):
            received.append(event)

        event_bus.subscribe(EventType.GAME_TIME_UPDATE, handler)
        event_bus.publish(Event(
            event_type=EventType.GAME_TIME_UPDATE,
            data={"time": 100.0},
            source="test"
        ))

        assert len(received) == 1
        assert received[0].event_type == EventType.GAME_TIME_UPDATE
        assert received[0].data["time"] == 100.0

    def test_multiple_subscribers(self, event_bus):
        received1 = []
        received2 = []

        event_bus.subscribe(EventType.CHAMPION_KILL, lambda e: received1.append(e))
        event_bus.subscribe(EventType.CHAMPION_KILL, lambda e: received2.append(e))

        event_bus.publish(Event(event_type=EventType.CHAMPION_KILL, data={}))

        assert len(received1) == 1
        assert len(received2) == 1

    def test_unsubscribe(self, event_bus):
        received = []

        def handler(event):
            received.append(event)

        event_bus.subscribe(EventType.GAME_ENDED, handler)
        event_bus.unsubscribe(EventType.GAME_ENDED, handler)
        event_bus.publish(Event(event_type=EventType.GAME_ENDED, data={}))

        assert len(received) == 0


class TestStateDiff:
    """StateDiff 测试"""

    def _make_snapshot(self, game_time, level=6, items=None, spells=None, events=None):
        from app.lol.snapshot import GameSnapshot, PlayerInfo

        snapshot = GameSnapshot()
        snapshot.game_time = game_time
        snapshot.is_game_running = True

        player = PlayerInfo(
            summoner_name="TestPlayer",
            champion_name="Ahri",
            team="ORDER",
            is_current_player=True,
            level=level,
            items=items or [{"itemID": 1001}],
            summoner_spells=spells or [{"displayName": "Flash", "cooldownRemaining": 0}],
        )
        snapshot.players = [player]
        snapshot.current_player = player
        snapshot.recent_events = events or []

        return snapshot

    def test_state_diff_produces_events(self):
        from app.lol.state_diff import StateDiff

        old = self._make_snapshot(
            game_time=300.0,
            level=6,
            items=[{"itemID": 1001}],
            spells=[{"displayName": "Flash", "cooldownRemaining": 0}],
        )

        new = self._make_snapshot(
            game_time=360.0,
            level=7,
            items=[{"itemID": 1001}, {"itemID": 1002}],
            spells=[{"displayName": "Flash", "cooldownRemaining": 280}],
            events=[
                {"EventName": "ChampionKill", "EventTime": 350, "KillerName": "TestPlayer", "VictimName": "Enemy1"},
                {"EventName": "DragonKill", "EventTime": 340, "DragonType": "fire", "KillerName": "TestPlayer"},
                {"EventName": "TurretKilled", "EventTime": 345, "TurretKilled": "Turret_T1_01", "KillerName": "TestPlayer"},
            ],
        )

        sd = StateDiff()
        sd._last_snapshot = old
        events = sd.diff(new)

        event_types = {e.event_type for e in events}

        # 验证 7 个关键事件都产生
        expected = {
            EventType.GAME_TIME_UPDATE,
            EventType.PLAYER_LEVEL_UP,
            EventType.CHAMPION_KILL,
            EventType.ITEM_CHANGED,
            EventType.PLAYER_SUMMONER_SPELL_USED,
            EventType.DRAGON_KILLED,
            EventType.TOWER_DESTROYED,
        }

        assert expected.issubset(event_types), f"缺少事件: {expected - event_types}"

    def test_events_reach_event_bus(self, event_bus):
        """验证 StateDiff 产生的事件能被 EventBus 正确接收"""
        from app.lol.state_diff import StateDiff

        received = []

        for et in [
            EventType.GAME_TIME_UPDATE,
            EventType.PLAYER_LEVEL_UP,
            EventType.CHAMPION_KILL,
            EventType.ITEM_CHANGED,
            EventType.PLAYER_SUMMONER_SPELL_USED,
            EventType.DRAGON_KILLED,
            EventType.TOWER_DESTROYED,
        ]:
            event_bus.subscribe(et, lambda e: received.append(e.event_type))

        old = self._make_snapshot(game_time=300.0)
        new = self._make_snapshot(
            game_time=360.0,
            level=7,
            items=[{"itemID": 1001}, {"itemID": 1002}],
            spells=[{"displayName": "Flash", "cooldownRemaining": 280}],
            events=[
                {"EventName": "ChampionKill", "EventTime": 350, "KillerName": "TestPlayer", "VictimName": "Enemy1"},
                {"EventName": "DragonKill", "EventTime": 340, "DragonType": "fire", "KillerName": "TestPlayer"},
                {"EventName": "TurretKilled", "EventTime": 345, "TurretKilled": "Turret_T1_01", "KillerName": "TestPlayer"},
            ],
        )

        sd = StateDiff()
        sd._last_snapshot = old
        events = sd.diff(new)

        for e in events:
            event_bus.publish(Event(event_type=e.event_type, data=e.data, source="test"))

        time.sleep(0.1)

        received_set = set(received)
        expected = {
            EventType.GAME_TIME_UPDATE,
            EventType.PLAYER_LEVEL_UP,
            EventType.CHAMPION_KILL,
            EventType.ITEM_CHANGED,
            EventType.PLAYER_SUMMONER_SPELL_USED,
            EventType.DRAGON_KILLED,
            EventType.TOWER_DESTROYED,
        }

        assert expected.issubset(received_set), f"EventBus 未收到事件: {expected - received_set}"


class TestGameAlarm:
    """GameAlarm 测试"""

    def test_engine_init(self):
        from app.lol.game_alarm import GameAlarmEngine
        engine = GameAlarmEngine({})
        assert engine is not None

    def test_create_custom_alarm(self):
        from app.lol.game_alarm import GameAlarmEngine
        engine = GameAlarmEngine({})
        alarm = engine.create_custom_alarm("测试提醒", duration=60.0, game_time=100.0)
        assert alarm.title == "测试提醒"
        assert alarm.game_time_start == 100.0
        assert alarm.game_time_end == 160.0

    def test_create_flash_alarm(self):
        from app.lol.game_alarm import GameAlarmEngine
        engine = GameAlarmEngine({})
        alarm = engine.create_flash_alarm("莎弥拉", game_time=100.0)
        assert "莎弥拉" in alarm.title
        assert "闪现" in alarm.title

    def test_create_spell_alarm(self):
        from app.lol.game_alarm import GameAlarmEngine
        engine = GameAlarmEngine({})
        alarm = engine.create_spell_alarm("莎弥拉", "W", cooldown=60.0, game_time=100.0)
        assert "莎弥拉" in alarm.title
        assert "W" in alarm.title

    def test_create_ward_alarm(self):
        from app.lol.game_alarm import GameAlarmEngine
        engine = GameAlarmEngine({})
        alarm = engine.create_ward_alarm("中路草丛", game_time=100.0)
        assert "眼位" in alarm.title

    def test_alarm_only_notifies_once(self):
        from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
        engine = GameAlarmEngine({})
        alarm = engine.create_custom_alarm("一次性提醒", duration=10.0, game_time=100.0)

        engine._current_game_time = 115.0
        engine._check_alarms(115.0)

        assert alarm.status == AlarmStatus.TRIGGERED
        assert alarm.triggered_at is not None

        # 再次检查不应该重复触发
        engine._check_alarms(120.0)
        active = engine.get_active_alarms()
        assert alarm not in active
