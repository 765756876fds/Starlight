"""修 1（P0）：alarm.custom 改现实时钟（wall 基准）。

覆盖：
- custom alarm 不传 game_time → time_base="wall"（现实时间，非对局也能触发）
- 显式传 game_time → time_base="game"（保持对局内计时语义，兼容 test_core.py）
- wall 线程（_wall_loop → _check_wall_alarms）只检查 wall 基准，不碰 game alarm
- 游戏开始/结束只清理 game 基准提醒，wall 日常提醒跨对局保留
"""
import time
from unittest.mock import MagicMock

from app.core.event_bus import EventType
from app.lol.game_alarm import GameAlarmEngine, AlarmStatus


def _engine_with_bus():
    """构造 engine + mock event_bus（隔离发布）。"""
    engine = GameAlarmEngine({})
    engine.event_bus = MagicMock()
    return engine


def test_custom_wall_triggers_without_game_update():
    """不传 game_time → wall 基准；无 GAME_TIME_UPDATE 也能触发（wall 线程驱动）。"""
    engine = _engine_with_bus()
    engine.start()
    try:
        alarm = engine.create_custom_alarm("喝水", duration=0.3, lead_time=0.0)
        assert alarm.time_base == "wall"

        deadline = time.time() + 2.0
        while alarm.status != AlarmStatus.TRIGGERED and time.time() < deadline:
            time.sleep(0.05)
        assert alarm.status == AlarmStatus.TRIGGERED
        # 发布了 ALARM_TRIGGERED
        published_types = [c.args[0].event_type for c in engine.event_bus.publish.call_args_list]
        assert EventType.ALARM_TRIGGERED in published_types
    finally:
        engine.stop()


def test_custom_wall_not_before_due():
    """wall 基准未到期不触发。"""
    engine = _engine_with_bus()
    engine.start()
    try:
        alarm = engine.create_custom_alarm("远提醒", duration=60.0, lead_time=0.0)
        time.sleep(1.2)
        assert alarm.status == AlarmStatus.ACTIVE
    finally:
        engine.stop()


def test_explicit_game_time_stays_game_base():
    """显式传 game_time → game 基准（对齐 test_core.py:224-227）。"""
    engine = _engine_with_bus()
    alarm = engine.create_custom_alarm("测试提醒", duration=60.0, game_time=100.0)
    assert alarm.time_base == "game"
    assert alarm.game_time_start == 100.0
    assert alarm.game_time_end == 160.0


def test_mixed_bases_check():
    """_check_alarms：game 到期项按 game 时间触发；wall 项按自身基准（monotonic）判断。"""
    engine = _engine_with_bus()
    game_alarm = engine.create_custom_alarm("对局提醒", duration=10.0, game_time=100.0)
    wall_alarm = engine.create_custom_alarm("喝水", duration=60.0)  # wall，未到期
    assert wall_alarm.time_base == "wall"

    engine._current_game_time = 115.0
    engine._check_alarms(115.0)

    assert game_alarm.status == AlarmStatus.TRIGGERED   # 对局内到期 → 触发
    assert wall_alarm.status == AlarmStatus.ACTIVE      # wall 未到期 → 不动


def test_wall_loop_only_checks_wall():
    """_check_wall_alarms 只碰 wall 基准；game alarm 不被 wall 线程触发。"""
    engine = _engine_with_bus()
    game_alarm = engine.create_custom_alarm("对局提醒", duration=60.0, game_time=100.0)
    wall_alarm = engine.create_custom_alarm("喝水", duration=0.2, lead_time=0.0)

    engine._current_game_time = 115.0   # stale：若 wall 线程查 game alarm，会误触发
    wall_alarm.game_time_start = time.monotonic() - 5.0   # 回拨：已到期 → 确定性触发
    engine._check_wall_alarms()

    assert wall_alarm.status == AlarmStatus.TRIGGERED
    assert game_alarm.status == AlarmStatus.ACTIVE   # wall 线程不碰 game alarm


def test_game_end_preserves_wall_alarms():
    """游戏结束：game 基准过期，wall 日常提醒保留。"""
    engine = _engine_with_bus()
    wall_alarm = engine.create_custom_alarm("喝水", duration=60.0)
    game_alarm = engine.create_custom_alarm("对局提醒", duration=60.0, game_time=100.0)

    engine._on_game_end(None)

    assert game_alarm.status == AlarmStatus.EXPIRED
    assert wall_alarm.status == AlarmStatus.ACTIVE


def test_game_start_preserves_wall_alarms():
    """游戏开始：只清 game 基准；wall 提醒跨对局保留（进游戏不误清）。"""
    engine = _engine_with_bus()
    wall_alarm = engine.create_custom_alarm("喝水", duration=60.0)
    game_alarm = engine.create_custom_alarm("对局提醒", duration=60.0, game_time=100.0)

    engine._on_game_start(None)

    assert game_alarm.status == AlarmStatus.EXPIRED
    assert wall_alarm.status == AlarmStatus.ACTIVE
    assert engine._current_game_time == 0.0


def test_serialization_has_time_base():
    """_alarm_to_dict 含 time_base。"""
    engine = _engine_with_bus()
    alarm = engine.create_custom_alarm("喝水", duration=60.0)
    d = engine._alarm_to_dict(alarm)
    assert d["time_base"] == "wall"
    assert d["remaining"] >= 0


def test_stop_joins_wall_thread():
    """stop 后 wall 线程退出（_wall_thread 置空）。"""
    engine = _engine_with_bus()
    engine.start()
    assert engine._wall_thread is not None
    engine.stop()
    assert engine._wall_thread is None
