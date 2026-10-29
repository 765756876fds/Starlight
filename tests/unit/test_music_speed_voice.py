import pytest
from app.voice.intent import RuleBasedIntent as R
from app.core.command_bus import CommandType, CommandBus, Command


def test_nine_speeds():
    cases = {
        "倍速零点五倍": 0.5,
        "倍速零点七五倍": 0.75,
        "倍速一倍": 1.0,
        "倍速一点二五倍": 1.25,
        "倍速一点五倍": 1.5,
        "倍速一点七五倍": 1.75,
        "倍速两倍": 2.0,
        "倍速二点五倍": 2.5,
        "倍速三倍": 3.0,
    }
    for text, expected in cases.items():
        it = R.match(text)
        assert it is not None, f"{text} 未匹配"
        assert it.intent_type == CommandType.MUSIC_SET_SPEED, f"{text} 路由错误: {it.intent_type}"
        assert it.params.get("speed") == expected, f"{text} speed 错误: {it.params}"


def test_normal_speed():
    for t in ["恢复正常速度", "正常速度", "倍速正常"]:
        it = R.match(t)
        assert it is not None and it.intent_type == CommandType.MUSIC_SET_SPEED
        assert it.params.get("speed") == 1.0


def test_invalid():
    for t in ["倍速十点倍", "倍速", "倍速一百倍"]:
        it = R.match(t)
        assert it is None or it.intent_type != CommandType.MUSIC_SET_SPEED or "speed" not in it.params, t


def test_music_commands_unaffected():
    assert R.match("音量五十").intent_type == CommandType.MUSIC_VOLUME_SET
    assert R.match("快进十秒").intent_type == CommandType.MUSIC_SEEK
    assert R.match("随机播放").intent_type == CommandType.MUSIC_SET_PLAY_MODE


def test_not_movie():
    for t in ["倍速一点五倍", "恢复正常速度"]:
        it = R.match(t)
        assert it.intent_type == CommandType.MUSIC_SET_SPEED


def test_bus_dispatch():
    bus = CommandBus()
    received = {}
    bus.register(CommandType.MUSIC_SET_SPEED, lambda c: received.update(c.params) or True)
    bus.execute(Command(command_type=CommandType.MUSIC_SET_SPEED, params={"speed": 1.5}))
    assert received.get("speed") == 1.5


def test_bus_missing_param_rejected():
    bus = CommandBus()
    received = []
    bus.register(CommandType.MUSIC_SET_SPEED, lambda c: received.append(c.params.get("speed")) if "speed" in c.params else False)
    bus.execute(Command(command_type=CommandType.MUSIC_SET_SPEED, params={}))
    assert received == []
