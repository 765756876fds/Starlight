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

