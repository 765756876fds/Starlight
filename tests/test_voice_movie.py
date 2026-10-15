"""包2b：语音控制电影——意图 / 命令 / 路由 / 状态测试。

覆盖：
- 意图：裸"播放XX"仍走音乐；"播放电影XX"/"看电影XX"/"放电影XX" → MOVIE_SEARCH_PLAY
- 命令：CommandType 新增 MOVIE_*；runtime 分发 MOVIE_SEARCH_PLAY → execute_async
- 路由：谁在播谁响应（MUSIC_PAUSE/RESUME 按电影播放态路由，防循环）
- 状态：movie_controller play/pause/resume/stop 写 state_store(MOVIE)（语音路由数据源）
- 音乐回归：无电影时暂停/继续 → MUSIC_* 命令零变化
"""
import unittest
from unittest.mock import MagicMock, patch

from app.voice.intent import RuleBasedIntent, Intent
from app.voice.runtime import VoiceRuntime
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
    """谁在播谁响应：暂停/继续按电影播放态路由。"""

    def test_pause_movie_playing(self):
        rt = _mk_rt(True, False)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_PAUSE)

    def test_pause_movie_paused_noop(self):
        # 电影暂停中 → 走音乐（音乐没播则 no-op，不报错）
        rt = _mk_rt(True, True)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MUSIC_PAUSE)

    def test_pause_music_only(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MUSIC_PAUSE)

    def test_resume_movie_paused(self):
        rt = _mk_rt(True, True)
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_RESUME)

    def test_resume_movie_playing_idempotent(self):
        rt = _mk_rt(True, False)
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MOVIE_RESUME)

    def test_resume_music_only(self):
        rt = _mk_rt(False, False)
        rt._execute_intent(Intent(CommandType.MUSIC_RESUME), "继续播放")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MUSIC_RESUME)

    def test_state_read_failure_falls_back_to_music(self):
        rt = VoiceRuntime.__new__(VoiceRuntime)
        rt.command_bus = MagicMock()
        rt.event_bus = MagicMock()
        rt.state_store = MagicMock()
        rt.state_store.get.side_effect = RuntimeError("store down")
        rt._execute_intent(Intent(CommandType.MUSIC_PAUSE), "暂停")
        cmd = rt.command_bus.execute.call_args[0][0]
        self.assertEqual(cmd.command_type, CommandType.MUSIC_PAUSE)

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


if __name__ == "__main__":
    unittest.main()
