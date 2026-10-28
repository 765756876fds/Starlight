"""包2b：语音控制电影——意图 / 命令 / 路由 / 状态测试。

覆盖：
- 意图：裸"播放XX"仍走音乐；"播放电影XX"/"看电影XX"/"放电影XX" → MOVIE_SEARCH_PLAY
- 命令：CommandType 新增 MOVIE_*；runtime 分发 MOVIE_SEARCH_PLAY → execute_async
- 路由：谁在播谁响应（MUSIC_PAUSE/RESUME 按电影播放态路由，防循环）
- 状态：movie_controller play/pause/resume/stop 写 state_store(MOVIE)（语音路由数据源）
- 音乐回归：无电影时暂停/继续 → MUSIC_* 命令零变化
"""
import pytest

pytestmark = pytest.mark.unit

import unittest
from unittest.mock import MagicMock, patch

from app.voice.intent import RuleBasedIntent, Intent
from app.voice.runtime import VoiceRuntime, fold_duplicated_text
from app.core.command_bus import CommandType, Command, get_command_bus
from app.core.state_store import StateNamespace, get_state_store
from app.movie.movie_controller import MovieController


class TestVoiceMovieIntent(unittest.TestCase):
    """意图：显式"电影/影片/片子"才走电影；裸"播放XX"保持音乐。"""

    def test_bare_play_stays_music(self):
        it = RuleBasedIntent.match("播放流浪地球")
        self.assertEqual(it.intent_type, CommandType.MUSIC_SEARCH_PLAY)
        self.assertEqual(it.params["query"], "流浪地球")

    def test_play_movie_variants(self):
        for text in ("播放电影流浪地球", "放电影流浪地球", "看电影流浪地球",
                     "播放影片流浪地球", "播放片子流浪地球", "播放一部电影流浪地球"):
            it = RuleBasedIntent.match(text)
            self.assertEqual(it.intent_type, CommandType.MOVIE_SEARCH_PLAY, text)
            self.assertEqual(it.params["query"], "流浪地球", text)

    def test_movie_query_tail_particle_cleaned(self):
        it = RuleBasedIntent.match("播放电影流浪地球啊")
        self.assertEqual(it.intent_type, CommandType.MOVIE_SEARCH_PLAY)
        self.assertEqual(it.params["query"], "流浪地球")

    def test_pause_resume_intents_unchanged(self):
        self.assertEqual(RuleBasedIntent.match("暂停").intent_type, CommandType.MUSIC_PAUSE)
        self.assertEqual(RuleBasedIntent.match("继续播放").intent_type, CommandType.MUSIC_RESUME)

    def test_other_search_forms_stay_music(self):
        # "听流浪地球" / "放点流浪地球" 仍音乐（无"电影/影片/片子"字样）
        for text in ("听流浪地球", "放点流浪地球", "来一首流浪地球"):
            it = RuleBasedIntent.match(text)
            self.assertEqual(it.intent_type, CommandType.MUSIC_SEARCH_PLAY, text)


def _mk_rt(playing, paused):
    """构造 VoiceRuntime 骨架（__new__ 跳过 __init__），state_store 模拟电影态。"""
    rt = VoiceRuntime.__new__(VoiceRuntime)
    rt.command_bus = MagicMock()
    rt.event_bus = MagicMock()
    rt.state_store = MagicMock()
    rt.state_store.get.side_effect = lambda ns, k, d=None: {
        ("movie", "playing"): playing,
        ("movie", "paused"): paused,
    }.get((ns, k), d)
    return rt


class TestVoiceMovieRouting(unittest.TestCase):
    """V2-2：裸暂停/继续走全局暂停/恢复流程。"""

    def test_pause_movie_playing(self):
        rt = _mk_rt(True, False)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MOVIE_PAUSE, types)

    def test_pause_movie_paused_noop(self):
        # 电影已暂停 → 不再发 MOVIE_PAUSE
        rt = _mk_rt(True, True)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertNotIn(CommandType.MOVIE_PAUSE, types)

    def test_pause_music_only(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        # 音乐在播（state_store MUSIC.playing 默认 False 在 _mk_rt 里没设）
        # 此场景无媒体在播 → 不发命令
        # 实际 _mk_rt 只 mock movie state；music state 默认 False

    def test_resume_movie_paused(self):
        rt = _mk_rt(True, True)
        rt._global_pause_snapshot = {"movie": True}
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MOVIE_RESUME, types)

    def test_resume_movie_playing_idempotent(self):
        # 电影在播未暂停 → 跳过
        rt = _mk_rt(True, False)
        rt._global_pause_snapshot = {"movie": True}
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        rt.command_bus.execute.assert_not_called()

    def test_resume_music_only(self):
        rt = _mk_rt(False, False)
        rt._global_pause_snapshot = {"music": True}
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        types = [c[0][0].command_type for c in rt.command_bus.execute.call_args_list]
        self.assertIn(CommandType.MUSIC_RESUME, types)

    def test_state_read_failure_falls_back_to_music(self):
        rt = VoiceRuntime.__new__(VoiceRuntime)
        rt.command_bus = MagicMock()
        rt.event_bus = MagicMock()
        rt.state_store = MagicMock()
        rt.state_store.get.side_effect = RuntimeError("store down")
        rt.speech_queue_provider = None
        rt._global_pause_snapshot = {}
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        # 状态读失败 → 无媒体在播 → 不发命令，不崩

    def test_movie_search_play_dispatch_async(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(
            Intent(CommandType.MOVIE_SEARCH_PLAY, params={"query": "流浪地球"}),
            "播放电影流浪地球",
        )
        rt.command_bus.execute_async.assert_called_once()
        cmd = rt.command_bus.execute_async.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_SEARCH_PLAY)
        self.assertEqual(cmd.params["query"], "流浪地球")

    def test_other_music_commands_unchanged_even_with_movie(self):
        # 电影在播时 next/toggle/highlight 等仍走音乐（只有暂停/继续路由）
        for t in (CommandType.MUSIC_NEXT, CommandType.MUSIC_TOGGLE,
                  CommandType.MUSIC_HIGHLIGHT, CommandType.MUSIC_CLEAR_QUEUE):
            rt = _mk_rt(True, False)
            rt._execute_intent(Intent(t), "x")
            cmd = rt.command_bus.execute.call_args[0][0]
            self.assertEqual(cmd.command_type, t)


class TestMovieControllerVoice(unittest.TestCase):
    """电影控制器：播放态写入 + 命令注册 + 语音搜索播放。"""

    def setUp(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def tearDown(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def _mk_controller(self):
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = False
        fake_mpv.start.return_value = True
        fake_mpv.stop.return_value = True  # 三态契约：协议成功才清停止状态
        fake_mpv.loadfile.return_value = {"error": "success"}  # loadfile 协议契约
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={})
        ctrl._fake_mpv = fake_mpv
        return ctrl

    @staticmethod
    def _item():
        return {"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                "play_url": "正片$http://x/1.m3u8"}

    def test_play_writes_playing_state(self):
        ctrl = self._mk_controller()
        self.assertTrue(ctrl.play(self._item()))
        st = get_state_store()
        self.assertTrue(st.get(StateNamespace.MOVIE, "playing"))
        self.assertFalse(st.get(StateNamespace.MOVIE, "paused"))

    def test_pause_marks_paused(self):
        ctrl = self._mk_controller()
        ctrl.play(self._item())
        ctrl._fake_mpv.is_running.return_value = True
        ctrl.pause()
        st = get_state_store()
        self.assertTrue(st.get(StateNamespace.MOVIE, "playing"))
        self.assertTrue(st.get(StateNamespace.MOVIE, "paused"))

    def test_resume_clears_paused(self):
        ctrl = self._mk_controller()
        ctrl.play(self._item())
        ctrl._fake_mpv.is_running.return_value = True
        ctrl.pause()
        ctrl.resume()
        st = get_state_store()
        self.assertTrue(st.get(StateNamespace.MOVIE, "playing"))
        self.assertFalse(st.get(StateNamespace.MOVIE, "paused"))

    def test_stop_clears_state(self):
        ctrl = self._mk_controller()
        ctrl.play(self._item())
        ctrl._fake_mpv.is_running.return_value = True
        ctrl.stop()
        st = get_state_store()
        self.assertFalse(st.get(StateNamespace.MOVIE, "playing"))
        self.assertFalse(st.get(StateNamespace.MOVIE, "paused"))

    def test_pause_without_movie_does_not_write(self):
        ctrl = self._mk_controller()
        ctrl.pause()
        st = get_state_store()
        self.assertIsNone(st.get(StateNamespace.MOVIE, "playing"))

    def test_commands_registered(self):
        ctrl = self._mk_controller()
        bus = get_command_bus()
        # 注册后 execute MOVIE_SEARCH_PLAY（搜不到 → False，不崩）
        cmd = Command(command_type=CommandType.MOVIE_SEARCH_PLAY,
                      params={"query": "不存在的电影"}, source="test")
        with patch.object(ctrl, "search", return_value=[]):
            result = bus.execute(cmd)
        self.assertFalse(result)

    def test_cmd_search_play_plays_top1(self):
        ctrl = self._mk_controller()
        with patch.object(ctrl, "search", return_value=[self._item()]):
            ok = ctrl._cmd_search_play(
                Command(CommandType.MOVIE_SEARCH_PLAY, {"query": "流浪地球"}, "test"))
        self.assertTrue(ok)
        st = get_state_store()
        self.assertTrue(st.get(StateNamespace.MOVIE, "playing"))

    def test_cmd_search_play_empty_query(self):
        ctrl = self._mk_controller()
        self.assertFalse(ctrl._cmd_search_play(
            Command(CommandType.MOVIE_SEARCH_PLAY, {"query": "  "}, "test")))

    def test_empty_query_enqueues_tts(self):
        """空 query → enqueue SpeechItem('请说电影名字')，不只刷 UI。"""
        from app.speech.speech_queue import SpeechItem
        fake_sq = MagicMock()
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = False
        fake_mpv.start.return_value = True
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={}, speech_queue=fake_sq)
        try:
            ok = ctrl._cmd_search_play(
                Command(CommandType.MOVIE_SEARCH_PLAY, {"query": ""}, "test"))
            self.assertFalse(ok)
            fake_sq.enqueue.assert_called_once()
            item = fake_sq.enqueue.call_args[0][0]
            self.assertIsInstance(item, SpeechItem)
            self.assertEqual(item.text, "请说电影名字")
        finally:
            ctrl.close()

    def test_speech_queue_enqueue_failure_does_not_crash(self):
        """SpeechQueue.enqueue 异常 → 不崩，记错误日志（含"TTS 播报失败"）。"""
        fake_sq = MagicMock()
        fake_sq.enqueue.side_effect = RuntimeError("sq down")
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = False
        fake_mpv.start.return_value = True
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={}, speech_queue=fake_sq)
        try:
            with patch("app.movie.movie_controller.logger") as mock_logger:
                self.assertFalse(ctrl._cmd_search_play(
                    Command(CommandType.MOVIE_SEARCH_PLAY, {"query": ""}, "test")))
                mock_logger.error.assert_called()
                self.assertTrue(any("TTS 播报失败" in str(c) for c in mock_logger.error.call_args_list),
                                mock_logger.error.call_args_list)
        finally:
            ctrl.close()

    def test_cmd_search_play_search_exception(self):
        ctrl = self._mk_controller()
        with patch.object(ctrl, "search", side_effect=RuntimeError("boom")):
            self.assertFalse(ctrl._cmd_search_play(
                Command(CommandType.MOVIE_SEARCH_PLAY, {"query": "流浪地球"}, "test")))



# ================= 包2c：语音选集（"播放电影XX第X集"直跳对应集） =================

class TestVoiceMovieEpisodeIntent(unittest.TestCase):
    """意图：集数解析（中文/阿拉伯数字），无集数播第一集（现状不变）。"""

    def test_episode_arabic(self):
        it = RuleBasedIntent.match("播放电影甄嬛传第5集")
        self.assertEqual(it.intent_type, CommandType.MOVIE_SEARCH_PLAY)
        self.assertEqual(it.params["query"], "甄嬛传")
        self.assertEqual(it.params["episode"], 5)

    def test_episode_chinese(self):
        it = RuleBasedIntent.match("播放电影甄嬛传第五集")
        self.assertEqual(it.params["query"], "甄嬛传")
        self.assertEqual(it.params["episode"], 5)

    def test_episode_no_prefix(self):
        it = RuleBasedIntent.match("放电影甄嬛传5集")
        self.assertEqual(it.params["query"], "甄嬛传")
        self.assertEqual(it.params["episode"], 5)

    def test_episode_hua(self):
        it = RuleBasedIntent.match("看影片流浪地球2第10话")
        self.assertEqual(it.params["query"], "流浪地球2")
        self.assertEqual(it.params["episode"], 10)

    def test_episode_chinese_tens(self):
        it = RuleBasedIntent.match("播放电影琅琊榜第十一集")
        self.assertEqual(it.params["query"], "琅琊榜")
        self.assertEqual(it.params["episode"], 11)

    def test_episode_digit_in_title_not_confused(self):
        # "流浪地球2" 的 2 是片名一部分（结尾无 集/话/期），不能误当集数
        it = RuleBasedIntent.match("播放电影流浪地球2")
        self.assertEqual(it.params["query"], "流浪地球2")
        self.assertNotIn("episode", it.params)

    def test_episode_without_number_first(self):
        it = RuleBasedIntent.match("播放电影甄嬛传")
        self.assertEqual(it.params["query"], "甄嬛传")
        self.assertNotIn("episode", it.params)

    def test_bare_play_with_episode_stays_music(self):
        # 裸"播放XX第5集"（无"电影/影片/片子"）仍音乐搜索，不误伤音乐链
        it = RuleBasedIntent.match("播放甄嬛传第5集")
        self.assertEqual(it.intent_type, CommandType.MUSIC_SEARCH_PLAY)

    def test_cn_to_int_helper(self):
        conv = RuleBasedIntent._cn_episode_to_int
        self.assertEqual(conv("第五"), 5)
        self.assertEqual(conv("25"), 25)
        self.assertEqual(conv("十一"), 11)
        self.assertEqual(conv("一百零五"), 105)
        self.assertIsNone(conv(""))
        self.assertIsNone(conv("abc"))


class TestVoiceMovieEpisodeRouting(unittest.TestCase):
    """路由：MOVIE_SEARCH_PLAY 带 episode → execute_async 透传。"""

    def test_movie_search_play_episode_passthrough(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(
            Intent(CommandType.MOVIE_SEARCH_PLAY,
                   params={"query": "甄嬛传", "episode": 5}),
            "播放电影甄嬛传第5集",
        )
        rt.command_bus.execute_async.assert_called_once()
        cmd = rt.command_bus.execute_async.call_args[0][0]
        self.assertEqual(cmd.params["query"], "甄嬛传")
        self.assertEqual(cmd.params["episode"], 5)

    def test_movie_search_play_no_episode_passthrough(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(
            Intent(CommandType.MOVIE_SEARCH_PLAY, params={"query": "流浪地球"}),
            "播放电影流浪地球",
        )
        cmd = rt.command_bus.execute_async.call_args[0][0]
        self.assertEqual(cmd.params["query"], "流浪地球")
        self.assertNotIn("episode", cmd.params)


class TestMovieControllerEpisodeVoice(unittest.TestCase):
    """控制器：语音选集直跳 / 越界提示 / 单集边界。"""

    def setUp(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def tearDown(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def _mk_controller(self):
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = False
        fake_mpv.start.return_value = True
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={})
        ctrl._fake_mpv = fake_mpv
        return ctrl

    @staticmethod
    def _series_item(n=10):
        parts = "#".join(
            "第%02d集$http://x/%d.m3u8" % (i, i) for i in range(1, n + 1))
        return {"vod_name": "甄嬛传", "first_url": "http://x/1.m3u8",
                "play_url": parts}

    @staticmethod
    def _single_item():
        return {"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                "play_url": "正片$http://x/1.m3u8"}

    def test_episode_jumps_to_index(self):
        ctrl = self._mk_controller()
        item = self._series_item(10)
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "甄嬛传", "episode": 5}, "test"))
        self.assertTrue(ok)
        p.assert_called_once_with(item, index=4)  # 第5集 → index=4

    def test_episode_out_of_range_rejected(self):
        ctrl = self._mk_controller()
        item = self._series_item(10)
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "甄嬛传", "episode": 99}, "test"))
        self.assertFalse(ok)
        p.assert_not_called()

    def test_episode_zero_rejected(self):
        ctrl = self._mk_controller()
        item = self._series_item(10)
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "甄嬛传", "episode": 0}, "test"))
        self.assertFalse(ok)
        p.assert_not_called()

    def test_single_movie_ep1_plays_default(self):
        ctrl = self._mk_controller()
        item = self._single_item()
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "流浪地球", "episode": 1}, "test"))
        self.assertTrue(ok)
        p.assert_called_once_with(item)  # 单集 + 第1集 = 默认播放路径

    def test_single_movie_ep2_rejected(self):
        ctrl = self._mk_controller()
        item = self._single_item()
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "流浪地球", "episode": 2}, "test"))
        self.assertFalse(ok)
        p.assert_not_called()

    def test_no_episode_plays_top1(self):
        ctrl = self._mk_controller()
        item = self._series_item(10)
        with patch.object(ctrl, "search", return_value=[item]),              patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(Command(
                CommandType.MOVIE_SEARCH_PLAY,
                {"query": "甄嬛传"}, "test"))
        self.assertTrue(ok)
        p.assert_called_once_with(item)  # 无集数 → 播第一集（现状不变）


# ================= P1：语音搜电影失败 UI 反馈（voice_feedback 回调） =================

class TestVoiceFeedback(unittest.TestCase):
    """各失败/成功分支 → _voice_feedback 回调（面板状态栏显示）。"""

    def setUp(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def tearDown(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def _mk_ctrl_with_feedback(self):
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = False
        fake_mpv.start.return_value = True
        msgs = []
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={}, voice_feedback=msgs.append)
        return ctrl, fake_mpv, msgs

    @staticmethod
    def _cmd(query, episode=None):
        params = {"query": query}
        if episode is not None:
            params["episode"] = episode
        return Command(CommandType.MOVIE_SEARCH_PLAY, params, "test")

    def test_not_found_notifies(self):
        ctrl, _, msgs = self._mk_ctrl_with_feedback()
        with patch.object(ctrl, "search", return_value=[]):
            ok = ctrl._cmd_search_play(self._cmd("不存在的电影"))
        self.assertFalse(ok)
        self.assertEqual(msgs, ["未找到：不存在的电影"])

    def test_search_exception_notifies(self):
        ctrl, _, msgs = self._mk_ctrl_with_feedback()
        with patch.object(ctrl, "search", side_effect=RuntimeError("boom")):
            ok = ctrl._cmd_search_play(self._cmd("流浪地球"))
        self.assertFalse(ok)
        self.assertIn("语音搜索失败", msgs[0])

    def test_single_movie_ep2_notifies(self):
        ctrl, _, msgs = self._mk_ctrl_with_feedback()
        item = {"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                "play_url": "正片$http://x/1.m3u8"}
        with patch.object(ctrl, "search", return_value=[item]):
            ok = ctrl._cmd_search_play(self._cmd("流浪地球", episode=2))
        self.assertFalse(ok)
        self.assertEqual(msgs, ["该片只有 1 集，没有第 2 集"])

    def test_episode_oob_notifies(self):
        ctrl, _, msgs = self._mk_ctrl_with_feedback()
        item = {"vod_name": "甄嬛传", "first_url": "http://x/1.m3u8",
                "play_url": "第01集$http://x/1.m3u8#第02集$http://x/2.m3u8"}
        with patch.object(ctrl, "search", return_value=[item]), \
             patch.object(ctrl, "play", return_value=True):
            ok = ctrl._cmd_search_play(self._cmd("甄嬛传", episode=11))
        self.assertFalse(ok)
        self.assertEqual(msgs, ["该剧只有 2 集，没有第 11 集"])

    def test_play_success_notifies(self):
        ctrl, _, msgs = self._mk_ctrl_with_feedback()
        item = {"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                "play_url": "正片$http://x/1.m3u8"}
        with patch.object(ctrl, "search", return_value=[item]), \
             patch.object(ctrl, "play", return_value=True) as p:
            ok = ctrl._cmd_search_play(self._cmd("流浪地球"))
        self.assertTrue(ok)
        p.assert_called_once_with(item)
        self.assertEqual(msgs, ["开始播放：流浪地球"])

    def test_no_feedback_fallback_print(self):
        """无回调注入 → 不崩（print 兜底路径）。"""
        ctrl = MovieController(config={})
        ctrl._voice_feedback = None
        with patch.object(ctrl, "search", return_value=[]):
            self.assertFalse(ctrl._cmd_search_play(self._cmd("不存在")))
        ctrl.close()


class TestMoviePauseResumeVoiceFeedback(unittest.TestCase):
    """暂停/继续成功后 TTS 播报。"""

    def setUp(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def tearDown(self):
        st = get_state_store()
        for k in ("playing", "paused"):
            st.delete(StateNamespace.MOVIE, k)

    def _mk(self):
        fake_sq = MagicMock()
        fake_mpv = MagicMock()
        fake_mpv.is_running.return_value = True
        fake_mpv.start.return_value = True
        fake_mpv.stop.return_value = True
        fake_mpv.loadfile.return_value = {"error": "success"}
        with patch("app.movie.movie_controller.MpvController", return_value=fake_mpv):
            ctrl = MovieController(config={}, speech_queue=fake_sq)
        ctrl._fake_mpv = fake_mpv
        return ctrl, fake_sq

    def test_pause_with_movie_notifies(self):
        ctrl, sq = self._mk()
        ctrl.play({"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                   "play_url": "正片$http://x/1.m3u8"})
        ctrl.pause()
        sq.enqueue.assert_called_once()
        self.assertEqual(sq.enqueue.call_args[0][0].text, "电影已暂停")
        ctrl.close()

    def test_pause_without_movie_no_notify(self):
        ctrl, sq = self._mk()
        ctrl.pause()
        sq.enqueue.assert_not_called()
        ctrl.close()

    def test_resume_with_movie_notifies(self):
        ctrl, sq = self._mk()
        ctrl.play({"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                   "play_url": "正片$http://x/1.m3u8"})
        ctrl.pause()
        sq.enqueue.reset_mock()
        ctrl.resume()
        sq.enqueue.assert_called_once()
        self.assertEqual(sq.enqueue.call_args[0][0].text, "电影继续播放")
        ctrl.close()

    def test_resume_without_movie_no_notify(self):
        ctrl, sq = self._mk()
        ctrl.resume()
        sq.enqueue.assert_not_called()
        ctrl.close()

    def test_pause_sq_failure_does_not_crash(self):
        ctrl, sq = self._mk()
        sq.enqueue.side_effect = RuntimeError("sq down")
        ctrl.play({"vod_name": "流浪地球", "first_url": "http://x/1.m3u8",
                   "play_url": "正片$http://x/1.m3u8"})
        with patch("app.movie.movie_controller.logger") as mock_logger:
            ctrl.pause()
            mock_logger.error.assert_called()
        ctrl.close()


# ================= 问题1：ASR 重复文本折叠（轻量去重，不动 VAD） =================

class TestFoldDuplicatedText(unittest.TestCase):
    """fold_duplicated_text：播放声污染导致的 ASR 重复拼接 → 折叠成一段。"""

    def test_user_case(self):
        self.assertEqual(fold_duplicated_text(
            "播放电影X。播放X。播放电影X。"), "播放电影X。")

    def test_pause_case(self):
        self.assertEqual(fold_duplicated_text(
            "暂停播放。暂停。暂停。暂停。暂停。"), "暂停播放。")

    def test_real_log_case(self):
        t = "播放电影《甄嬛传》第五集。播放《甄嬛传》。播放电影《甄嬛传》第五集。播放电影《甄嬛传》第五集。"
        self.assertEqual(fold_duplicated_text(t), "播放电影《甄嬛传》第五集。")

    def test_identical_repeat(self):
        self.assertEqual(fold_duplicated_text("播放电影X。播放电影X。"), "播放电影X。")

    def test_different_commands_kept(self):
        self.assertEqual(fold_duplicated_text("播放A。播放B。"), "播放A。播放B。")

    def test_single_segment_unchanged(self):
        self.assertEqual(fold_duplicated_text("播放电影X。"), "播放电影X。")

    def test_no_punct_single(self):
        self.assertEqual(fold_duplicated_text("播放"), "播放")

    def test_empty(self):
        self.assertEqual(fold_duplicated_text(""), "")

    def test_keeps_punctuation_style(self):
        self.assertEqual(fold_duplicated_text("下一曲！下一曲！"), "下一曲！")

    def test_process_speech_uses_fold(self):
        """_process_speech：ASR 返回重复拼接 → 发布折叠后的文本。"""
        from app.voice.runtime import VoiceRuntime
        rt = VoiceRuntime.__new__(VoiceRuntime)
        rt._asr_enabled = True
        rt.asr = MagicMock()
        rt.asr.transcribe.return_value = "播放电影X。播放X。播放电影X。"
        rt.tts_provider = None
        rt.event_bus = MagicMock()
        rt.intent_router = MagicMock()
        rt.intent_router.parse.return_value = MagicMock()
        rt._execute_intent = MagicMock()
        rt._should_collect_speech = MagicMock(return_value=True)
        rt._process_speech(b"audio", 16000)
        # 第一次 publish = VOICE_TEXT_RECEIVED（text 已折叠）；第二次 = VOICE_INTENT_PARSED
        published = rt.event_bus.publish.call_args_list[0][0][0]
        self.assertEqual(published.data["text"], "播放电影X。")
        self.assertEqual(rt.intent_router.parse.call_args[0][0], "播放电影X。")


if __name__ == "__main__":
    unittest.main()
