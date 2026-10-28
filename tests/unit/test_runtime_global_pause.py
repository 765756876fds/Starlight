"""V2-2 全局暂停/继续 runtime 测试。"""
import pytest
pytestmark = pytest.mark.unit

import unittest
from unittest.mock import MagicMock

from app.voice.runtime import VoiceRuntime
from app.core.command_bus import CommandType
from app.core.state_store import StateNamespace


def _fake_mpv(paused=None, running=True):
    mpv = MagicMock()
    mpv.is_running.return_value = running
    mpv.get_property.return_value = paused
    return mpv


def _mk_rt(music_playing=False, movie_playing=False, movie_paused=False,
           music_paused=None, movie_paused_state=None,
           music_gen=0, movie_gen=0):
    rt = VoiceRuntime.__new__(VoiceRuntime)
    rt.command_bus = MagicMock()
    rt.event_bus = MagicMock()
    rt.state_store = MagicMock()
    rt.state_store.get.side_effect = lambda ns, k, d=None: {
        (StateNamespace.MUSIC, "playing"): music_playing,
        (StateNamespace.MOVIE, "playing"): movie_playing,
        (StateNamespace.MOVIE, "paused"): movie_paused,
    }.get((ns, k), d)
    rt.speech_queue_provider = None
    rt._global_pause_snapshot = {}
    fm = MagicMock(); fm.is_running.return_value = True; fm.get_property.return_value = music_paused
    fv = MagicMock(); fv.is_running.return_value = True; fv.get_property.return_value = movie_paused_state
    rt.music_mpv_provider = lambda: fm
    rt.movie_mpv_provider = lambda: fv
    mc = MagicMock(); mc._playback_gen = music_gen
    mv = MagicMock(); mv._playback_gen = movie_gen
    rt.music_controller_provider = lambda: mc
    rt.movie_controller_provider = lambda: mv
    return rt


class TestGlobalPause(unittest.TestCase):
    def test_no_media_playing(self):
        rt = _mk_rt()
        rt._global_pause()
        rt.command_bus.execute.assert_not_called()
        self.assertEqual(rt._global_pause_snapshot, {})

    def test_only_music_playing(self):
        rt = _mk_rt(music_playing=True, music_paused=True)
        rt._global_pause()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MUSIC_PAUSE, types)
        self.assertNotIn(CommandType.MOVIE_PAUSE, types)
        self.assertIn("music", rt._global_pause_snapshot)

    def test_only_movie_playing(self):
        rt = _mk_rt(movie_playing=True, movie_paused=False, movie_paused_state=True)
        rt._global_pause()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MOVIE_PAUSE, types)
        self.assertNotIn(CommandType.MUSIC_PAUSE, types)

    def test_both_playing(self):
        rt = _mk_rt(music_playing=True, movie_playing=True, movie_paused=False,
                    music_paused=True, movie_paused_state=True)
        rt._global_pause()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MUSIC_PAUSE, types)
        self.assertIn(CommandType.MOVIE_PAUSE, types)

    def test_movie_already_paused_not_paused_again(self):
        rt = _mk_rt(movie_playing=True, movie_paused=True)
        rt._global_pause()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertNotIn(CommandType.MOVIE_PAUSE, types)

    def test_ipc_none_not_recorded(self):
        rt = _mk_rt(music_playing=True, music_paused=None)
        rt._global_pause()
        self.assertNotIn("music", rt._global_pause_snapshot)


class TestGlobalResume(unittest.TestCase):
    def test_empty_snapshot(self):
        rt = _mk_rt()
        rt._global_resume()
        rt.command_bus.execute.assert_not_called()

    def test_resume_music(self):
        rt = _mk_rt(music_paused=True)
        rt._global_pause_snapshot = {"music": {"gen": 0}}
        rt._global_resume()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MUSIC_RESUME, types)
        self.assertEqual(rt._global_pause_snapshot, {})

    def test_resume_skips_when_already_playing(self):
        rt = _mk_rt(music_paused=False)
        rt._global_pause_snapshot = {"music": {"gen": 0}}
        rt._global_resume()
        rt.command_bus.execute.assert_not_called()

    def test_resume_skips_when_unknown(self):
        rt = _mk_rt(music_paused=None)
        rt._global_pause_snapshot = {"music": {"gen": 0}}
        rt._global_resume()
        rt.command_bus.execute.assert_not_called()

    def test_resume_movie(self):
        rt = _mk_rt(movie_paused_state=True)
        rt._global_pause_snapshot = {"movie": {"gen": 0}}
        rt._global_resume()
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MOVIE_RESUME, types)

    def test_resume_movie_not_paused_skips(self):
        rt = _mk_rt(movie_paused_state=False)
        rt._global_pause_snapshot = {"movie": {"gen": 0}}
        rt._global_resume()
        rt.command_bus.execute.assert_not_called()

    def test_resume_skips_when_gen_mismatch(self):
        rt = _mk_rt(music_paused=True, music_gen=5)
        rt._global_pause_snapshot = {"music": {"gen": 3}}
        rt._global_resume()
        rt.command_bus.execute.assert_not_called()

    def test_new_pause_replaces_snapshot(self):
        rt = _mk_rt(music_playing=True, music_paused=True)
        rt._global_pause()
        self.assertIn("music", rt._global_pause_snapshot)
        # 第二次全局暂停（电影在播）替换快照
        rt._global_pause_snapshot = {}
        rt.state_store.get.side_effect = lambda ns, k, d=None: {
            (StateNamespace.MUSIC, "playing"): False,
            (StateNamespace.MOVIE, "playing"): True,
            (StateNamespace.MOVIE, "paused"): False,
        }.get((ns, k), d)
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = True
        fake_mpv.get_property.return_value = True
        rt.movie_mpv_provider = lambda: fake_mpv
        rt._global_pause()
        self.assertNotIn("music", rt._global_pause_snapshot)


if __name__ == "__main__":
    unittest.main()
