import pytest
from app.voice.intent import RuleBasedIntent as R
from app.core.command_bus import CommandType, CommandBus, Command


def test_playlist_play_intent():
    it = R.match("播放我的歌单")
    assert it.intent_type == CommandType.MUSIC_PLAY_PLAYLIST
    assert it.params.get("name") == "我的"


def test_create_playlist_intent():
    it = R.match("新建歌单叫工作")
    assert it.intent_type == CommandType.MUSIC_CREATE_PLAYLIST
    assert it.params.get("name") == "工作"


def test_add_to_playlist_intent():
    it = R.match("把这首歌加到工作歌单")
    assert it.intent_type == CommandType.MUSIC_ADD_TO_PLAYLIST
    assert it.params.get("name") == "工作"


def test_add_next_intent():
    it = R.match("把七里香放到下一首")
    assert it.intent_type == CommandType.MUSIC_ADD_NEXT
    assert it.params.get("query") == "七里香"


def test_add_next_variants():
    assert R.match("把库里之歌放在下一首").params["query"] == "库里之歌"
    assert R.match("把七里香插到下一首").params["query"] == "七里香"
    assert R.match("把《库里之歌》放在下一首").params["query"] == "库里之歌"


def test_add_next_no_false_trigger():
    assert R.match("把门关好") is None


def test_rank_index_intent():
    it = R.match("播放当前榜单第3首")
    assert it.intent_type == CommandType.MUSIC_PLAY_RANK_INDEX
    assert it.params.get("index") == 2  # 1-based → 0-based


def test_rank_index_chinese():
    assert R.match("播放当前榜单第二首").params["index"] == 1
    assert R.match("播放当前榜单第三首").params["index"] == 2
    assert R.match("播放当前榜单第10首").params["index"] == 9


def test_no_conflict_with_song_search():
    # "播放七里香" 仍走搜索，不是歌单
    it = R.match("播放七里香")
    assert it is not None
    assert it.intent_type != CommandType.MUSIC_PLAY_PLAYLIST


def test_bus_dispatch_playlist():
    bus = CommandBus()
    received = {}
    bus.register(CommandType.MUSIC_PLAY_PLAYLIST, lambda c: received.update(c.params) or True)
    bus.execute(Command(command_type=CommandType.MUSIC_PLAY_PLAYLIST, params={"name": "测试"}))
    assert received.get("name") == "测试"
