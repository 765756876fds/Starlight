"""Phase 1-D: Objective Reminder 测试"""
import pytest

pytestmark = pytest.mark.unit

import pytest
from unittest.mock import MagicMock

from app.core.event_bus import EventBus, Event, EventType, get_event_bus
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus
from app.lol.objective_timer import ObjectiveTimer


@pytest.fixture
def bus():
    b = EventBus()
    return b


@pytest.fixture
def alarm(bus):
    # GameAlarmEngine uses get_event_bus() singleton, so we need to patch it
    import app.lol.game_alarm as ga_mod
    orig = ga_mod.get_event_bus
    ga_mod.get_event_bus = lambda: bus
    eng = GameAlarmEngine({})
    yield eng
    ga_mod.get_event_bus = orig


@pytest.fixture
def timer(alarm, bus):
    import app.lol.objective_timer as ot_mod
    orig = ot_mod.get_event_bus
    ot_mod.get_event_bus = lambda: bus
    t = ObjectiveTimer(alarm)
    t.start()
    yield t
    t.stop()
    ot_mod.get_event_bus = orig


def _time_update(bus, game_time):
    bus.publish(Event(
        event_type=EventType.GAME_TIME_UPDATE,
        data={"game_time": game_time},
        source="test"
    ))


def _game_start(bus):
    bus.publish(Event(
        event_type=EventType.GAME_STARTED,
        data={},
        source="test"
    ))


def _game_end(bus):
    bus.publish(Event(
        event_type=EventType.GAME_ENDED,
        data={},
        source="test"
    ))


def _dragon_kill(bus, game_time, dragon_type="", killer_team=""):
    bus.publish(Event(
        event_type=EventType.DRAGON_KILLED,
        data={"game_time": game_time, "dragon_type": dragon_type,
              "killer_team": killer_team},
        source="test"
    ))


def _baron_kill(bus, game_time):
    bus.publish(Event(
        event_type=EventType.BARON_KILLED,
        data={"game_time": game_time},
        source="test"
    ))


class TestFixedResources:
    """固定资源首次刷新"""

    def test_void_grubs_first_reminder(self, timer, alarm, bus):
        """7:00 提醒巢虫（8:00 刷新、提前 60s）"""
        _game_start(bus)
        _time_update(bus, 60)   # 先排程（60 < 所有提醒点；直接跳提醒点会被 _schedule_for 跳过）
        _time_update(bus, 420)  # 7:00
        active = alarm.get_active_alarms()
        # 巢虫 alarm 应已触发
        triggered = [a for a in alarm.alarms.values()
                     if a.data.get("objective") == "void_grubs"]
        assert len(triggered) == 1
        assert triggered[0].status == AlarmStatus.TRIGGERED

    def test_herald_first_reminder(self, timer, alarm, bus):
        """14:00 提醒先锋（15:00 刷新、提前 60s）"""
        _game_start(bus)
        _time_update(bus, 60)   # 先排程
        _time_update(bus, 840)  # 14:00
        herald_alarms = [a for a in alarm.alarms.values()
                         if a.data.get("objective") == "herald"]
        assert len(herald_alarms) == 1
        assert herald_alarms[0].status == AlarmStatus.TRIGGERED

    def test_dragon_first_reminder(self, timer, alarm, bus):
        """4:20 提醒小龙（5:00 刷新、提前 40s）"""
        _game_start(bus)
        _time_update(bus, 60)   # 先排程
        _time_update(bus, 260)  # 4:20
        dragon_alarms = [a for a in alarm.alarms.values()
                         if a.data.get("objective") == "dragon"]
        assert len(dragon_alarms) == 1
        assert dragon_alarms[0].status == AlarmStatus.TRIGGERED

    def test_baron_first_reminder(self, timer, alarm, bus):
        """19:20 提醒大龙（20:00 刷新、提前 40s）"""
        _game_start(bus)
        _time_update(bus, 60)    # 先排程
        _time_update(bus, 1160)  # 19:20
        baron_alarms = [a for a in alarm.alarms.values()
                        if a.data.get("objective") == "baron"]
        assert len(baron_alarms) == 1
        assert baron_alarms[0].status == AlarmStatus.TRIGGERED


class TestDragonRespawn:
    """小龙重复刷新"""

    def test_dragon_respawn_after_kill(self, timer, alarm, bus):
        """6:00 击杀 -> 11:00 刷新 -> 10:20 提醒"""
        _game_start(bus)
        # 第一条龙已提醒
        _time_update(bus, 260)
        # 6:00 击杀
        _dragon_kill(bus, 360)
        # 检查是否安排了下一条
        pending = [a for a in alarm.alarms.values()
                   if a.data.get("objective") == "dragon" and a.status == AlarmStatus.ACTIVE]
        assert len(pending) == 1
        # 下一条应在 360+300=660s 刷新，提醒在 620s
        assert abs(pending[0].game_time_end - 660) < 1
        # 10:20 时应触发
        _time_update(bus, 620)
        assert pending[0].status == AlarmStatus.TRIGGERED

    def test_dragon_second_respawn(self, timer, alarm, bus):
        """12:00 再次击杀 -> 17:00 刷新 -> 16:20 提醒"""
        _game_start(bus)
        _time_update(bus, 260)
        # 第一次击杀 6:00
        _dragon_kill(bus, 360)
        _time_update(bus, 620)  # 触发第一次提醒
        # 第二次击杀 12:00
        _dragon_kill(bus, 720)
        pending = [a for a in alarm.alarms.values()
                   if a.data.get("objective") == "dragon" and a.status == AlarmStatus.ACTIVE]
        assert len(pending) == 1
        # 下一条应在 720+300=1020s
        assert abs(pending[0].game_time_end - 1020) < 1


class TestBaronRespawn:
    """大龙重复刷新"""

    def test_baron_respawn_after_kill(self, timer, alarm, bus):
        """22:00 击杀 -> 28:00 刷新 -> 27:20 提醒"""
        _game_start(bus)
        _time_update(bus, 1160)  # 首次提醒
        # 22:00 击杀
        _baron_kill(bus, 1320)
        pending = [a for a in alarm.alarms.values()
                   if a.data.get("objective") == "baron" and a.status == AlarmStatus.ACTIVE]
        assert len(pending) == 1
        # 下一次应在 1320+360=1680s
        assert abs(pending[0].game_time_end - 1680) < 1


class TestElder:
    """远古龙生命周期"""

    def test_elder_after_soul(self, timer, alarm, bus):
        """4条龙后 -> +6min 首次 Elder"""
        _game_start(bus)
        for i in range(4):
            _dragon_kill(bus, 600 + i * 100, killer_team="Blue")  # 同队 4 条触发 Soul
        elder = [a for a in alarm.alarms.values()
                 if a.data.get("objective") == "elder"]
        assert len(elder) >= 1
        # 第4条龙在 900s，Elder spawn = 900+360=1260
        assert abs(elder[-1].game_time_end - 1260) < 1

    def test_elder_respawn(self, timer, alarm, bus):
        """Elder 击杀后 +6min"""
        _game_start(bus)
        # 先达成 Soul
        for i in range(4):
            _dragon_kill(bus, 600 + i * 100)
        # Elder 击杀
        _dragon_kill(bus, 1300, dragon_type="Elder")
        elder_pending = [a for a in alarm.alarms.values()
                         if a.data.get("objective") == "elder" and a.status == AlarmStatus.ACTIVE]
        assert len(elder_pending) >= 1
        # 下一次 Elder = 1300+360=1660
        assert abs(elder_pending[-1].game_time_end - 1660) < 1


class TestNoCatchUp:
    """不补播"""

    def test_late_start_no_reminder(self, alarm, bus):
        """7:30 才启动，巢虫 7:00 提醒已过"""
        alarm._current_game_time = 450  # 7:30
        result = alarm.schedule_objective_reminder(
            "60秒后巢虫刷新", 480, 60, {"objective": "void_grubs"}
        )
        assert result is None  # 不创建

    def test_no_catchup_dragon(self, alarm, bus):
        """9:50 才恢复，龙 9:20 提醒已过"""
        alarm._current_game_time = 590  # 9:50
        result = alarm.schedule_objective_reminder(
            "40秒后小龙刷新", 600, 40, {"objective": "dragon"}
        )
        assert result is None


class TestGameEndCancellation:
    """GameEnd 取消未来提醒"""

    def test_game_end_cancels(self, timer, alarm, bus):
        """大龙提醒 24:20, GameEnd 23:00"""
        _game_start(bus)
        # 安排一个 24:20 的提醒（1460s - 40 = 1420 触发）
        alarm.schedule_objective_reminder(
            "40秒后大龙刷新", 1460, 40, {"objective": "baron"}
        )
        active_before = len(alarm.get_active_alarms())
        assert active_before >= 1
        # GameEnd
        _game_end(bus)
        # 所有 alarm 应标记为 EXPIRED
        for a in alarm.alarms.values():
            assert a.status == AlarmStatus.EXPIRED


class TestImmediateEventsMuted:
    """即时事件静音"""

    def test_combat_events_dropped(self):
        from app.speech.speech_judge import SpeechJudge, SpeechLevel
        levels = SpeechJudge.EVENT_LEVELS
        # 所有即时战斗事件应为 DROP
        for evt in [EventType.CHAMPION_KILL, EventType.FIRST_BLOOD,
                    EventType.MULTI_KILL, EventType.ACE,
                    EventType.TOWER_DESTROYED, EventType.DRAGON_KILLED,
                    EventType.HERALD_KILLED, EventType.BARON_KILLED]:
            assert levels.get(evt) == SpeechLevel.DROP, f"{evt} not muted"
        # ALARM_TRIGGERED 仍然可播报
        assert levels.get(EventType.ALARM_TRIGGERED) == SpeechLevel.B


class TestAlarmSpeechPath:
    """ALARM_TRIGGERED -> SpeechJudge -> 文本"""

    def test_alarm_text(self):
        from app.speech.speech_judge import SpeechJudge
        judge = SpeechJudge({})
        event = Event(
            event_type=EventType.ALARM_TRIGGERED,
            data={
                "alarm": {"title": "40秒后小龙刷新"},
                "remaining": 38,
            }
        )
        text = judge._generate_speech_text(event, 1)
        assert text == "40秒后小龙刷新"



class TestGetNextTimers:
    def test_empty_before_start(self):
        from app.lol.objective_timer import ObjectiveTimer
        class FakeAlarm:
            _current_game_time = 0
            def schedule_objective_reminder(self, *a, **kw): pass
        ot = ObjectiveTimer(FakeAlarm())
        assert ot.get_next_timers() == []

    def test_none_event_guard(self):
        """防御：_on_game_start(None) 不抛异常、不排程（生产 EventBus 恒传 Event）"""
        from app.lol.objective_timer import ObjectiveTimer
        class FakeAlarm:
            _current_game_time = 0
            def schedule_objective_reminder(self, *a, **kw): pass
        ot = ObjectiveTimer(FakeAlarm())
        ot._on_game_start(None)
        assert ot.get_next_timers() == []

    def test_after_game_start(self):
        from app.lol.objective_timer import ObjectiveTimer
        from app.core.event_bus import Event, EventType
        class FakeAlarm:
            _current_game_time = 0
            def schedule_objective_reminder(self, *a, **kw): pass
        ot = ObjectiveTimer(FakeAlarm())
        ot._on_game_start(Event(
            event_type=EventType.GAME_STARTED, data={"game_time": 30.0}))
        names = [t["name"] for t in ot.get_next_timers()]
        assert "小龙" in names
        assert "大龙" in names
        assert "先锋" in names

    def test_dragon_respawn_remaining(self):
        from app.lol.objective_timer import ObjectiveTimer
        from app.core.event_bus import Event, EventType
        class FakeAlarm:
            _current_game_time = 0
            def schedule_objective_reminder(self, *a, **kw): pass
        ot = ObjectiveTimer(FakeAlarm())
        ot._on_game_start(Event(
            event_type=EventType.GAME_STARTED, data={"game_time": 30.0}))
        class E:
            data = {"dragon_type": "normal"}
            def get(self, k, d=None): return 200
        ot._on_dragon_killed(E())
        ot.game_alarm._current_game_time = 400
        timers = {t["name"]: t["seconds_remaining"] for t in ot.get_next_timers()}
        assert abs(timers["小龙"] - 100) < 1
