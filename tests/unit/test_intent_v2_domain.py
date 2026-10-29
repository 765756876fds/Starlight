"""V2-1 明确领域命令与电影误路由修复测试。"""
import pytest

pytestmark = pytest.mark.unit

import unittest
from unittest.mock import MagicMock

from app.voice.intent import RuleBasedIntent, Intent
from app.voice.runtime import VoiceRuntime
from app.core.command_bus import CommandType


class TestMovieExplicitCommands:
    def test_pause_movie(self):
        for t in ("暂停电影", "电影暂停", "暂停影片", "影片暂停"):
            m = RuleBasedIntent.match(t)
            assert m and m.intent_type == CommandType.MOVIE_PAUSE, t

    def test_resume_movie(self):
        for t in ("继续电影", "电影继续", "恢复电影", "继续影片"):
            m = RuleBasedIntent.match(t)
            assert m and m.intent_type == CommandType.MOVIE_RESUME, t

    def test_pause_music_explicit(self):
        for t in ("暂停音乐", "音乐暂停"):
            m = RuleBasedIntent.match(t)
            assert m and m.intent_type == CommandType.MUSIC_PAUSE, t

    def test_resume_music_explicit(self):
        for t in ("继续音乐", "音乐继续", "继续放歌"):
            m = RuleBasedIntent.match(t)
            assert m and m.intent_type == CommandType.MUSIC_RESUME, t


class TestPlayMovieRouting:
    def test_play_movie_empty_query_goes_movie_not_music(self):
        """'播放电影' 必须走电影搜索（空 query），不得走音乐搜索"电影"。"""
        m = RuleBasedIntent.match("播放电影")
        assert m is not None
        assert m.intent_type == CommandType.MOVIE_SEARCH_PLAY
        assert m.params.get("query", "") == ""

    def test_play_movie_with_name(self):
        m = RuleBasedIntent.match("播放电影流浪地球")
        assert m and m.intent_type == CommandType.MOVIE_SEARCH_PLAY
        assert m.params["query"] == "流浪地球"

    def test_play_song_with_movie_word_in_title(self):
        """'播放歌曲黑白电影' → MUSIC_SEARCH_PLAY query='黑白电影'（剥掉"歌曲"前缀）。"""
        m = RuleBasedIntent.match("播放歌曲黑白电影")
        assert m and m.intent_type == CommandType.MUSIC_SEARCH_PLAY
        assert m.params["query"] == "黑白电影"

    def test_bare_play_movie_word_in_title_stays_music(self):
        """'播放黑白电影'（无"电影"前缀在"播放"后紧跟）→ 音乐搜索"黑白电影"。"""
        m = RuleBasedIntent.match("播放黑白电影")
        assert m and m.intent_type == CommandType.MUSIC_SEARCH_PLAY
        assert m.params["query"] == "黑白电影"

    def test_song_starting_with_ge_not_stripped(self):
        """以"歌"开头的合法歌名（如"歌声"）不得被误删首字。"""
        m = RuleBasedIntent.match("播放歌声")
        assert m and m.intent_type == CommandType.MUSIC_SEARCH_PLAY
        assert m.params["query"] == "歌声"

    def test_song_type_prefix_stripped(self):
        for text, expected in [
            ("播放歌曲黑白电影", "黑白电影"),
            ("播放一首七里香", "七里香"),
            ("播放这首周杰伦", "周杰伦"),
            ("播放那首情歌", "情歌"),
            ("播放曲子回家", "回家"),
        ]:
            m = RuleBasedIntent.match(text)
            assert m and m.intent_type == CommandType.MUSIC_SEARCH_PLAY, text
            assert m.params["query"] == expected, text


class TestBareCommandsUnchanged:
    def test_bare_pause_still_music_pause(self):
        m = RuleBasedIntent.match("暂停")
        assert m and m.intent_type == CommandType.MUSIC_PAUSE

    def test_bare_resume_still_music_resume(self):
        m = RuleBasedIntent.match("继续")
        assert m and m.intent_type == CommandType.MUSIC_RESUME


def _mk_rt(playing=False, paused=False):
    rt = VoiceRuntime.__new__(VoiceRuntime)
    rt.command_bus = MagicMock()
    rt.event_bus = MagicMock()
    rt.state_store = MagicMock()
    rt.state_store.get.side_effect = lambda ns, k, d=None: {
        ("movie", "playing"): playing,
        ("movie", "paused"): paused,
    }.get((ns, k), d)
    return rt


class TestRuntimeDispatch(unittest.TestCase):
    """集成：意图 → runtime → CommandBus，验证最终命令类型正确。"""

    def test_pause_movie_dispatches_movie_pause(self):
        rt = _mk_rt(playing=True, paused=False)
        rt._execute_intent(Intent(CommandType.MOVIE_PAUSE), "暂停电影")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_PAUSE)
        # 不得误发 MUSIC_PAUSE
        all_types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertNotIn(CommandType.MUSIC_PAUSE, all_types)

    def test_resume_movie_dispatches_movie_resume(self):
        rt = _mk_rt(playing=True, paused=True)
        rt._execute_intent(Intent(CommandType.MOVIE_RESUME), "继续电影")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_RESUME)

    def test_next_episode_dispatches_even_when_movie_is_paused(self):
        rt = _mk_rt(playing=True, paused=True)
        rt._execute_intent(Intent(CommandType.MOVIE_NEXT_EPISODE), "下一集")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_NEXT_EPISODE)

    def test_movie_command_failure_reports_feedback(self):
        rt = _mk_rt(playing=True, paused=False)
        rt.command_bus.execute.return_value = False
        rt._enqueue_speak = MagicMock()

        rt._execute_intent(Intent(CommandType.MOVIE_PAUSE), "暂停电影")

        rt._enqueue_speak.assert_called_once_with(
            "电影操作未成功", "voice_movie_control")

    def test_play_movie_empty_query_dispatches_movie_not_music(self):
        rt = _mk_rt()
        it = RuleBasedIntent.match("播放电影")
        assert it.intent_type == CommandType.MOVIE_SEARCH_PLAY
        rt._execute_intent(it, "播放电影")
        # 走电影 async，不走音乐 search
        rt.command_bus.execute_async.assert_called_once()
        cmd = rt.command_bus.execute_async.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_SEARCH_PLAY)
        self.assertEqual(cmd.params["query"], "")
        # 不得调音乐 search_play
        rt.command_bus.execute.assert_not_called()

    def test_play_song_strips_prefix_and_searches_music(self):
        rt = _mk_rt()
        it = RuleBasedIntent.match("播放歌曲黑白电影")
        assert it.intent_type == CommandType.MUSIC_SEARCH_PLAY
        assert it.params["query"] == "黑白电影"


class TestV24HighlightRuleFix:
    def test_movie_high_not_stolen_by_music_highlight(self):
        it = RuleBasedIntent.match("播放电影高潮")
        assert it.intent_type == CommandType.MOVIE_SEARCH_PLAY
        assert it.params["query"] == "高潮"

    def test_music_highlight_still_works(self):
        it = RuleBasedIntent.match("播放高潮")
        assert it.intent_type == CommandType.MUSIC_HIGHLIGHT

    def test_negative_sentence_not_trigger_highlight(self):
        assert RuleBasedIntent.match("不要播放电影高潮") is None

    def test_bare_gaochao_not_trigger(self):
        assert RuleBasedIntent.match("高潮") is None
        assert RuleBasedIntent.match("副歌") is None

    def test_next_episode_prevails(self):
        assert RuleBasedIntent.match("下一集").intent_type == CommandType.MOVIE_NEXT_EPISODE

    def test_next_song_prevails(self):
        assert RuleBasedIntent.match("下一首").intent_type == CommandType.MUSIC_NEXT


class TestV242FalseTriggerGuard:
    @pytest.mark.parametrize("text", [
        "不要切歌", "别切歌", "先别切歌",
        "不要暂停电影", "别继续播放",
        "不要播放电影高潮", "不要停止音乐",
        "今天这首歌还挺好听", "我不想看电影",
        "这个电影名字叫下一集", "我刚才说的不是让你切歌",
    ])
    def test_negation_and_chat_return_unknown(self, text):
        assert RuleBasedIntent.match(text) is None

    @pytest.mark.parametrize("text, expected", [
        ("下一首", CommandType.MUSIC_NEXT),
        ("上一首", CommandType.MUSIC_PREV),
        ("下一集", CommandType.MOVIE_NEXT_EPISODE),
        ("上一集", CommandType.MOVIE_PREV_EPISODE),
        ("暂停电影", CommandType.MOVIE_PAUSE),
        ("继续电影", CommandType.MOVIE_RESUME),
        ("播放高潮", CommandType.MUSIC_HIGHLIGHT),
        ("停止音乐", CommandType.MUSIC_STOP),
    ])
    def test_explicit_commands_still_work(self, text, expected):
        assert RuleBasedIntent.match(text).intent_type == expected

    def test_stop_tts_still_works(self):
        assert RuleBasedIntent.match("停止说话").intent_type == "system.stop_tts"


class TestV243PlayerNextArbitration:
    def test_next_one_is_player_next(self):
        assert RuleBasedIntent.match("下一个").intent_type == CommandType.PLAYER_NEXT

    def test_explicit_music_next_still_music(self):
        assert RuleBasedIntent.match("下一首").intent_type == CommandType.MUSIC_NEXT

    def test_explicit_movie_next_episode_still_movie(self):
        assert RuleBasedIntent.match("下一集").intent_type == CommandType.MOVIE_NEXT_EPISODE

    def test_player_next_routes_movie_when_movie_playing_music_paused(self):
        rt = _mk_rt()
        rt.movie_mpv_provider = lambda: _FakeMpv(paused=False)
        rt.music_mpv_provider = lambda: _FakeMpv(paused=True)
        it = RuleBasedIntent.match("下一个")
        rt._execute_intent(it, "下一个")
        types = [call[0][0].command_type for call in rt.command_bus.execute.call_args_list]
        assert CommandType.MOVIE_NEXT_EPISODE in types

    def test_player_next_routes_music_when_music_playing_movie_paused(self):
        rt = _mk_rt()
        rt.movie_mpv_provider = lambda: _FakeMpv(paused=True)
        rt.music_mpv_provider = lambda: _FakeMpv(paused=False)
        it = RuleBasedIntent.match("下一个")
        rt._execute_intent(it, "下一个")
        types = [call[0][0].command_type for call in rt.command_bus.execute.call_args_list]
        assert CommandType.MUSIC_NEXT in types

    def test_player_next_silent_on_unknown(self):
        rt = _mk_rt()
        rt.movie_mpv_provider = lambda: None
        rt.music_mpv_provider = lambda: _FakeMpv(paused=False)
        it = RuleBasedIntent.match("下一个")
        rt._execute_intent(it, "下一个")
        rt.command_bus.execute.assert_not_called()

    def test_player_next_silent_both_playing(self):
        rt = _mk_rt()
        rt.movie_mpv_provider = lambda: _FakeMpv(paused=False)
        rt.music_mpv_provider = lambda: _FakeMpv(paused=False)
        it = RuleBasedIntent.match("下一个")
        rt._execute_intent(it, "下一个")
        rt.command_bus.execute.assert_not_called()


class _FakeMpv:
    def __init__(self, paused):
        self._paused = paused
    def is_running(self): return True
    def get_property(self, name, timeout=1.0): return self._paused


class TestV244MusicSeek:
    @pytest.mark.parametrize("text, expected", [
        ("快进十秒", 10),
        ("快进10秒", 10),
        ("前进五秒", 5),
        ("后退五秒", -5),
        ("倒退五秒", -5),
        ("快进1秒", 1),
        ("快进60秒", 60),
    ])
    def test_seek_relative(self, text, expected):
        it = RuleBasedIntent.match(text)
        assert it.intent_type == CommandType.MUSIC_SEEK
        assert it.params["seconds"] == expected
        assert it.params["mode"] == "relative"

    @pytest.mark.parametrize("text", [
        "快进61秒", "快进0秒", "快进一百秒", "快进十", "快进秒",
        "快进abc秒", "不要快进十秒", "今天快进一下",
    ])
    def test_seek_rejects_invalid(self, text):
        assert RuleBasedIntent.match(text) is None


class TestV2452MovieStop:
    def test_stop_movie(self):
        assert RuleBasedIntent.match("停止电影").intent_type == CommandType.MOVIE_STOP

    def test_stop_music_unchanged(self):
        assert RuleBasedIntent.match("停止音乐").intent_type == CommandType.MUSIC_STOP

    def test_stop_tts_unchanged(self):
        assert RuleBasedIntent.match("停止说话").intent_type == "system.stop_tts"
        assert RuleBasedIntent.match("停止").intent_type == "system.stop_tts"

    def test_negative_stop_movie_unknown(self):
        assert RuleBasedIntent.match("不要停止电影") is None


class TestV2455VolumeChinese:
    @pytest.mark.parametrize("text, expected", [
        ("音量50", 50),
        ("音量五十", 50),
        ("音量0", 0),
        ("音量100", 100),
    ])
    def test_volume_set(self, text, expected):
        it = RuleBasedIntent.match(text)
        assert it.intent_type == CommandType.MUSIC_VOLUME_SET
        assert it.params["volume"] == expected

    def test_volume_101_no_param(self):
        it = RuleBasedIntent.match("音量101")
        assert it.intent_type == CommandType.MUSIC_VOLUME_SET
        assert "volume" not in it.params







