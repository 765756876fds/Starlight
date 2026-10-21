"""
包2a 测试：剧集选集判据 + 自动连播（idle 上升沿检测）——mock mpv，不依赖真实播放器。
"""
import pytest

pytestmark = pytest.mark.unit

import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from unittest.mock import MagicMock, patch

from app.movie.movie_controller import MovieController, split_play_url


def _make_ctrl(**kw):
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://source.test/api", music=kw.pop("music", None))
        ctrl.mpv = M.return_value
        return ctrl, M.return_value


# ---------- 选集判据 ----------

def test_is_multi_episode_by_segments():
    """段数 > 1 → 剧集（主判据）。"""
    it = {"vod_name": "甄嬛传", "vod_remarks": "全76集",
          "play_url": "第01集$u1#第02集$u2#第03集$u3"}
    assert MovieController.is_multi_episode(it) is True


def test_is_multi_episode_by_remarks():
    """段数 1 但 remarks 含 全X集 → 剧集（辅助判据）。"""
    it = {"vod_name": "x", "vod_remarks": "全40集", "play_url": "第01集$u1"}
    assert MovieController.is_multi_episode(it) is True


def test_is_multi_episode_single_movie():
    """电影：单段 + 无集数 remarks → 非剧集（直接播）。"""
    it = {"vod_name": "流浪地球", "vod_remarks": "高清",
          "play_url": "正片$https://a/index.m3u8"}
    assert MovieController.is_multi_episode(it) is False


def test_is_multi_episode_no_play_url():
    assert MovieController.is_multi_episode({}) is False
    assert MovieController.is_multi_episode(None) is False


def test_episodes_splits():
    ctrl, _ = _make_ctrl()
    it = {"play_url": "第01集$u1#第02集$u2"}
    assert ctrl.episodes(it) == [("第01集", "u1"), ("第02集", "u2")]
    assert ctrl.episodes({"play_url": "正片$u"}) == [("正片", "u")]
    assert ctrl.episodes({}) == []
    assert ctrl.episodes(None) == []


# ---------- play(index) 选集播放 ----------

def test_play_index_loads_that_episode():
    ctrl, mpv = _make_ctrl()
    mpv.is_running.return_value = True
    it = {"vod_name": "甄嬛传", "first_url": "u1",
          "play_url": "第01集$u1#第02集$u2#第03集$u3"}
    assert ctrl.play(it, index=1) is True
    mpv.loadfile.assert_called_once_with("u2")
    assert ctrl._current_index == 1


def test_play_index_defaults_first():
    ctrl, mpv = _make_ctrl()
    mpv.is_running.return_value = True
    it = {"vod_name": "甄嬛传", "first_url": "u1",
          "play_url": "第01集$u1#第02集$u2"}
    assert ctrl.play(it, index=None) is True
    mpv.loadfile.assert_called_once_with("u1")


def test_play_index_out_of_range():
    ctrl, mpv = _make_ctrl()
    it = {"vod_name": "甄嬛传", "first_url": "u1", "play_url": "第01集$u1"}
    assert ctrl.play(it, index=5) is False
    mpv.loadfile.assert_not_called()


def test_play_no_url():
    ctrl, _ = _make_ctrl()
    assert ctrl.play({"vod_name": "x", "first_url": ""}) is False
    assert ctrl.play(None) is False


# ---------- 自动连播 ----------

def _playing_ctrl(it=None):
    ctrl, mpv = _make_ctrl()
    mpv.is_running.return_value = True
    mpv.get_idle_active.return_value = False
    it = it or {"vod_name": "甄嬛传", "first_url": "u1",
                "play_url": "第01集$u1#第02集$u2#第03集$u3"}
    assert ctrl.play(it) is True
    ctrl._last_idle = False  # 已在播
    return ctrl, mpv


def test_tick_idle_rise_advances_next():
    """播完（idle 上升沿）→ 自动下一集。"""
    ctrl, mpv = _playing_ctrl()
    mpv.get_idle_active.return_value = True
    ctrl.tick()
    mpv.loadfile.assert_called_with("u2")
    assert ctrl._current_index == 1


def test_tick_still_playing_no_advance():
    ctrl, mpv = _playing_ctrl()
    mpv.get_idle_active.return_value = False
    ctrl.tick()
    mpv.loadfile.assert_called_once_with("u1")  # 只有初始 play 一次


def test_tick_last_episode_stops_not_loops():
    """末集播完 → 停（不循环）。"""
    it = {"vod_name": "x", "first_url": "u1", "play_url": "第01集$u1#第02集$u2"}
    ctrl, mpv = _playing_ctrl(it)
    # 手动切到第 2 集（末集）
    ctrl.play(it, index=1)
    mpv.loadfile.reset_mock()
    mpv.get_idle_active.return_value = True
    ctrl.tick()
    mpv.loadfile.assert_not_called()
    assert ctrl._current is None


def test_tick_auto_next_disabled():
    """开关关 → 播完不连播。"""
    ctrl, mpv = _playing_ctrl()
    ctrl.set_auto_next(False)
    mpv.loadfile.reset_mock()
    mpv.get_idle_active.return_value = True
    ctrl.tick()
    mpv.loadfile.assert_not_called()


def test_tick_no_current_after_stop():
    """手动 stop → 重置 idle 状态，不再连播。"""
    ctrl, mpv = _playing_ctrl()
    mpv.get_idle_active.return_value = True
    ctrl.stop()
    mpv.loadfile.reset_mock()
    ctrl.tick()
    mpv.loadfile.assert_not_called()
    assert ctrl._last_idle is True


def test_first_play_no_false_advance():
    """首次播放不误触发：初始 _last_idle=True，play 后置 False。"""
    ctrl, mpv = _make_ctrl()
    mpv.is_running.return_value = True
    mpv.get_idle_active.return_value = False
    assert ctrl._last_idle is True
    it = {"vod_name": "x", "first_url": "u1", "play_url": "第01集$u1#第02集$u2"}
    ctrl.play(it)
    assert ctrl._last_idle is False


def test_auto_next_default_on():
    ctrl, _ = _make_ctrl()
    assert ctrl.is_auto_next() is True
    ctrl.set_auto_next(False)
    assert ctrl.is_auto_next() is False
