"""V2-1 明确领域命令与电影误路由修复测试。"""
import pytest

pytestmark = pytest.mark.unit

from app.voice.intent import RuleBasedIntent
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
        """'播放歌曲黑白电影' 仍走音乐搜索，query 不被破坏。"""
        m = RuleBasedIntent.match("播放歌曲黑白电影")
        assert m and m.intent_type == CommandType.MUSIC_SEARCH_PLAY
        assert m.params["query"] == "歌曲黑白电影"


class TestBareCommandsUnchanged:
    def test_bare_pause_still_music_pause(self):
        m = RuleBasedIntent.match("暂停")
        assert m and m.intent_type == CommandType.MUSIC_PAUSE

    def test_bare_resume_still_music_resume(self):
        m = RuleBasedIntent.match("继续")
        assert m and m.intent_type == CommandType.MUSIC_RESUME
