# -*- coding: utf-8 -*-
"""包2c第二批测试：海报墙（网格/切换/占位/tooltip/详情/双击播）+ 详情页剥标签 + hls-bitrate + 分辨率。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch

import pytest

from PySide6.QtWidgets import QApplication, QListWidget

_app = QApplication.instance() or QApplication([])

from app.music.mpv import MpvController
from app.ui.panels import movie_panel as mp
from app.ui.panels.movie_panel import (
    _MovieDetailDialog,
    _get_poster,
    _make_poster_placeholder,
    _posters_cache,
    _strip_html,
    MoviePanel,
)


# ---------- mpv 画质参数（video 分支加、音乐分支不加） ----------

def _launch_args(video=False, watch_later_dir=None):
    import subprocess as _sp

    with patch.object(_sp, "Popen") as m_popen:
        m_popen.return_value = MagicMock()
        mpv = MpvController(sys.executable, video=video,
                            watch_later_dir=watch_later_dir)
        mpv._pipe = MagicMock()
        mpv.ipc_pipe = "mpv-test-1"
        with patch.object(mpv, "_wait_for_pipe"):
            with patch.object(mpv, "_init_properties"):
                ok = mpv._launch()
                assert ok
        args = m_popen.call_args[0][0]
        return args


def test_mpv_video_args_have_hls_bitrate_max():
    args = _launch_args(video=True)
    assert "--hls-bitrate=max" in args


def test_mpv_no_video_args_no_hls_bitrate():
    args = _launch_args(video=False)
    assert not any(a.startswith("--hls-bitrate") for a in args)


def test_mpv_video_keeps_previous_flags():
    args = _launch_args(video=True, watch_later_dir="D:/wl/movie")
    assert "--force-window=yes" in args
    assert "--osc=no" in args
    assert "--hls-bitrate=max" in args
    assert "--watch-later-directory=D:/wl/movie" in args


# ---------- mpv video-params 查询 ----------

def _running_mpv():
    mpv = MpvController("mpv")
    mpv._running = True
    mpv._process = MagicMock()
    mpv._process.poll.return_value = None
    return mpv


def test_get_video_params_ok():
    mpv = _running_mpv()
    with patch.object(mpv, "get_property", return_value={"w": 1920, "h": 1080, "format": "h264"}):
        assert mpv.get_video_params() == {"w": 1920, "h": 1080, "format": "h264"}


def test_get_video_params_not_running():
    mpv = MpvController("mpv")
    mpv._running = False
    assert mpv.get_video_params() == {}


def test_get_video_params_non_dict():
    mpv = _running_mpv()
    with patch.object(mpv, "get_property", return_value=None):
        assert mpv.get_video_params() == {}
    with patch.object(mpv, "get_property", side_effect=RuntimeError("ipc dead")):
        assert mpv.get_video_params() == {}


# ---------- controller get_resolution ----------

def _ctrl():
    from app.movie.movie_controller import MovieController
    c = MovieController.__new__(MovieController)
    c.mpv = MagicMock()
    return c


def test_controller_resolution_parsed():
    c = _ctrl()
    c.mpv.get_video_params.return_value = {"w": 1920, "h": 1080}
    assert c.get_resolution() == "1920x1080"


def test_controller_resolution_empty():
    c = _ctrl()
    c.mpv.get_video_params.return_value = {}
    assert c.get_resolution() == ""
    c.mpv.get_video_params.side_effect = RuntimeError("x")
    assert c.get_resolution() == ""


# ---------- 详情页字段解析（HTML 剥标签） ----------

def test_strip_html_basic():
    assert _strip_html("<p>你好</p>") == "你好"
    assert _strip_html("<p><span>a</span> <b>b</b></p>") == "a b"


def test_strip_html_unescape_and_ws():
    assert _strip_html("<p>a&nbsp;&amp;&lt;b&gt;</p>") == "a &<b>"
    assert _strip_html("  line1\n\n  line2  ") == "line1 line2"
    assert _strip_html(None) == ""
    assert _strip_html("") == ""


# ---------- 海报缓存 ----------

def test_poster_cache_memory_hit():
    _posters_cache.clear()
    with patch.object(mp, "_POSTER_DIR", new=__import__("pathlib").Path("nonexistent-dir")):
        assert _get_poster("999") is None
    pix = _make_poster_placeholder("测试")
    _posters_cache["999"] = pix
    assert _get_poster("999") is pix


def test_poster_cache_disk_hit(tmp_path):
    _posters_cache.clear()
    from pathlib import Path
    p = Path("x.jpg")
    (tmp_path / p).write_bytes(b"x")
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(mp, "QPixmap") as m_pix:
            m_pix.return_value.isNull.return_value = False
            assert _get_poster("x") is m_pix.return_value
            assert "x" in _posters_cache
    _posters_cache.clear()


def test_poster_placeholder_not_null():
    pix = _make_poster_placeholder("流浪地球")
    assert not pix.isNull()
    assert pix.width() == 140
    assert pix.height() == 210  # 竖版 2:3


# ---------- 面板：海报墙 ----------

class _FakeMovie:
    def __init__(self):
        self.play_calls = []

    def search(self, kw):
        return []

    def is_playing(self):
        return False

    def is_auto_next(self):
        return True

    def set_auto_next(self, on):
        pass

    def play(self, it, index=None):
        self.play_calls.append((it, index))
        return True

    def episodes(self, it):
        return [("第01集", "u1")]

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 100.0

    def get_resolution(self):
        return "1920x1080"


_ITEMS = [
    {"vod_id": 1, "vod_name": "流浪地球", "vod_score": "7.9", "vod_year": "2019",
     "vod_remarks": "正片", "vod_pic": "https://x/1.jpg",
     "vod_director": "郭帆", "vod_actor": "吴京", "vod_content": "<p>地球流浪</p>"},
    {"vod_id": 2, "vod_name": "奥本海默", "vod_score": "8.8", "vod_year": "2023",
     "vod_remarks": "", "vod_pic": ""},
]


def _panel():
    movie = _FakeMovie()
    p = MoviePanel(movie=movie)
    return p, movie


def test_panel_default_poster_mode():
    p, _ = _panel()
    assert p._poster_mode is True
    assert p.stack_results.currentWidget() is p.list_posters
    assert p.btn_view.isChecked() is True
    assert p.btn_view.text() == "海报"


def test_panel_toggle_view():
    p, _ = _panel()
    p._on_toggle_view()
    assert p._poster_mode is False
    assert p.stack_results.currentWidget() is p.list_results
    assert p.btn_view.text() == "列表"
    assert p.btn_view.isChecked() is False
    p._on_toggle_view()
    assert p.stack_results.currentWidget() is p.list_posters


def test_panel_posters_rendered_with_placeholder_and_tooltip():
    p, _ = _panel()
    with patch.object(p, "_start_poster_jobs") as m_jobs:
        p._on_search_done(list(_ITEMS))
        assert p.list_results.count() == 2
        assert p.list_posters.count() == 2
        assert p.stack_results.currentWidget() is p.list_posters
        # 占位 icon 非空（海报图未下载 → 灰底占位）
        for i in range(2):
            it = p.list_posters.item(i)
            assert not it.icon().isNull()
            data = it.data(0x0100)  # Qt.UserRole
            assert data["vod_name"]
        # tooltip：片名 + 评分 + 年份 + 备注
        tip0 = p.list_posters.item(0).toolTip()
        assert "流浪地球" in tip0 and "评分 7.9" in tip0 and "2019" in tip0 and "正片" in tip0
        # 无 pic → 不启下载线程
        m_jobs.assert_called_once()
        assert m_jobs.call_args[0][0] == [("1", "https://x/1.jpg")]


def test_panel_poster_worker_updates_icon(tmp_path):
    """下载完成后 _on_poster_done 更新对应项 icon（含磁盘缓存写入）。"""
    p, _ = _panel()
    with patch.object(p, "_start_poster_jobs"):
        p._on_search_done(list(_ITEMS))
    # 造一张真图
    from PySide6.QtGui import QPixmap
    fake = tmp_path / "1.jpg"
    QPixmap(20, 30).save(str(fake))
    _posters_cache.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        p._on_poster_done("1", str(fake))
        assert str(1) in _posters_cache
        assert not p.list_posters.item(0).icon().isNull()
    _posters_cache.clear()


def test_panel_click_opens_detail_after_timer():
    p, _ = _panel()
    with patch.object(p, "_start_poster_jobs"):
        p._on_search_done(list(_ITEMS))
    with patch.object(p, "_show_detail") as m_show:
        p._on_item_clicked(p.list_posters.item(0))
        assert p._click_timer.isActive()
        p._click_timer.timeout.emit()  # 模拟 300ms 到
        assert m_show.call_count == 1
        assert p._pending_click_item is None  # 单击已消费，防重复开详情
        p._click_timer.stop()


def test_panel_double_click_plays_and_cancels_detail():
    p, movie = _panel()
    with patch.object(p, "_start_poster_jobs"):
        p._on_search_done(list(_ITEMS))
    with patch.object(p, "_show_detail") as m_show:
        p._on_item_clicked(p.list_posters.item(1))
        assert p._click_timer.isActive()
        p._on_item_activated(p.list_posters.item(1))  # 双击
        assert not p._click_timer.isActive()
        assert movie.play_calls  # 直接播放（单集）
        m_show.assert_not_called()


def test_panel_detail_dialog_fields():
    p, _ = _panel()
    it = _ITEMS[0]
    dlg = _MovieDetailDialog(it, p)
    text = dlg.lbl_info.text()
    assert "流浪地球" in text
    assert "评分 7.9" in text
    assert "2019" in text
    assert "导演: 郭帆" in text
    assert "演员: 吴京" in text
    assert "地球流浪" in text  # 简介已剥标签
    assert "<p>" not in text
    assert dlg.btn_play.text() == "播放"
    assert dlg.lbl_pic.pixmap() is not None


def test_panel_detail_dialog_play_button():
    p, movie = _panel()
    with patch.object(p, "_start_poster_jobs"):
        p._on_search_done(list(_ITEMS))
    it = _ITEMS[0]
    dlg = _MovieDetailDialog(it, p)
    dlg._on_play()
    assert movie.play_calls  # 单集 → _play_item → play


# ---------- 分辨率显示（tick） ----------

def test_tick_updates_resolution_label():
    p, movie = _panel()
    with patch.object(p, "_start_poster_jobs"):
        p._on_search_done(list(_ITEMS))
    assert p.lbl_res.text() == ""
    p._tick()  # is_playing False → 清空
    assert p.lbl_res.text() == ""
    movie.is_playing = lambda: True
    p._tick()
    assert p.lbl_res.text() == "1920x1080"
