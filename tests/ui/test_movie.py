"""
电影链测试：CMS 解析 / play_url 拆分 / 搜索降级 / mpv 参数化 / 互斥不循环 / 面板渲染
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui

from unittest.mock import MagicMock, patch

from app.core.event_bus import Event, EventType, get_event_bus
from app.movie.movie_controller import (
    MovieController, first_play_url, parse_cms_response, split_play_url,
)
from app.music.mpv import MpvController


# ---------- CMS 响应解析 ----------

def test_parse_cms_response_json_body():
    """Content-Type 标 html 但 body 是 JSON → 按 body 首字符解析。"""
    text = '{"code":1,"list":[{"vod_name":"x"}]}'
    assert parse_cms_response(text)["code"] == 1
    assert parse_cms_response(text)["list"][0]["vod_name"] == "x"


def test_parse_cms_response_html_body():
    assert parse_cms_response("<html><body>error</body></html>") == {}


def test_parse_cms_response_empty_and_bad_json():
    assert parse_cms_response("") == {}
    assert parse_cms_response("   ") == {}
    assert parse_cms_response("{bad json") == {}


# ---------- vod_play_url 拆分 ----------

def test_split_play_url_multi():
    parts = split_play_url("流浪地球2$https://a/index.m3u8#4K版$https://b/index.m3u8")
    assert parts == [("流浪地球2", "https://a/index.m3u8"),
                     ("4K版", "https://b/index.m3u8")]


def test_split_play_url_single():
    assert split_play_url("星际穿越$https://s/index.m3u8") == [
        ("星际穿越", "https://s/index.m3u8")]


def test_split_play_url_no_dollar_or_empty():
    assert split_play_url("no-dollar-url") == []
    assert split_play_url("") == []
    assert split_play_url(None) == []
    assert split_play_url("名字$") == []   # 空 URL 段跳过


def test_first_play_url():
    assert first_play_url("A$u1#B$u2") == ("A", "u1")
    assert first_play_url("") is None
    assert first_play_url(None) is None


# ---------- 搜索 ----------

def _make_ctrl(**kw):
    with patch("app.movie.movie_controller.MpvController") as M:
        ctrl = MovieController(source_url="http://source.test/api", music=kw.pop("music", None))
        # 模拟真实 mpv JSON IPC：loadfile 成功返回 error=success 的响应字典。
        # MagicMock 不是有效的协议响应，生产代码应将其视为未确认/失败。
        M.return_value.loadfile.return_value = {"error": "success"}
        return ctrl, M.return_value


def test_search_ok():
    fake_resp = MagicMock()
    fake_resp.text = ('{"code":1,"total":1,"list":[{"vod_id":1,"vod_name":"流浪地球2",'
                      '"vod_remarks":"HD","vod_play_url":"流浪地球2$https://a/i.m3u8#4K$https://b/i.m3u8"}]}')
    with patch("app.movie.movie_controller.requests.get", return_value=fake_resp) as m_get:
        ctrl, _ = _make_ctrl()
        items = ctrl.search("流浪地球")
    m_get.assert_called_once()
    assert len(items) == 1
    assert items[0]["vod_name"] == "流浪地球2"
    assert items[0]["vod_remarks"] == "HD"
    assert items[0]["first_name"] == "流浪地球2"
    assert items[0]["first_url"] == "https://a/i.m3u8"
    ctrl.close()


def test_search_empty_result():
    fake_resp = MagicMock()
    fake_resp.text = '{"code":1,"total":0,"list":[]}'
    with patch("app.movie.movie_controller.requests.get", return_value=fake_resp):
        ctrl, _ = _make_ctrl()
        assert ctrl.search("不存在的电影") == []
        ctrl.close()


def test_search_blank_kw():
    ctrl, _ = _make_ctrl()
    assert ctrl.search("") == []
    assert ctrl.search("   ") == []
    ctrl.close()


def test_search_network_error_fallback():
    """多源策略 C：网络失败不再抛异常——换源/降级返回 [] + last_search_error。"""
    with patch("app.movie.movie_controller.requests.get",
               side_effect=Exception("timeout")):
        ctrl, _ = _make_ctrl()
        out = ctrl.search("流浪地球")
        assert out == []
        assert "单源" in ctrl.last_search_error  # 单源兼容：失败原因带源名


def test_search_items_include_vod_pic():
    """P0-1：search 精选 items 含 vod_pic（海报墙数据源；实测 CMS 有值、直连可下）。"""
    fake_resp = MagicMock()
    fake_resp.text = ('{"code":1,"total":2,"list":['
                      '{"vod_id":1,"vod_name":"A","vod_pic":"https://p/1.jpg",'
                      '"vod_play_url":"正片$https://a/i.m3u8"},'
                      '{"vod_id":2,"vod_name":"B","vod_pic":"",'
                      '"vod_play_url":"正片$https://b/i.m3u8"}]}')
    with patch("app.movie.movie_controller.requests.get", return_value=fake_resp):
        ctrl, _ = _make_ctrl()
        items = ctrl.search("A")
    assert items[0]["vod_pic"] == "https://p/1.jpg"
    assert items[1]["vod_pic"] == ""
    ctrl.close()


# ---------- mpv 参数化（音乐链零变化） ----------

def test_mpv_video_default_false():
    mpv = MpvController("mpv")
    assert mpv.video is False


def test_mpv_video_true():
    mpv = MpvController("mpv", video=True)
    assert mpv.video is True


def test_mpv_video_launch_args():
    """video=True → 出视频窗口（--force-window=yes），无 --no-video。"""
    with patch("app.music.mpv.subprocess.Popen") as m_popen, \
         patch("app.music.mpv.os.path.exists", return_value=True), \
         patch("app.music.mpv.open", create=True) as m_open:
        m_open.return_value = MagicMock()
        mpv = MpvController(sys.executable, video=True)
        assert mpv.start(taskkill=False, pipe_timeout=1.0, prop_timeout=0.3)
        args = m_popen.call_args[0][0]
        assert "--force-window=yes" in args
        assert "--no-video" not in args


def test_mpv_no_video_launch_args():
    """默认（音乐链）→ --no-video 纯音频，无视频窗口。"""
    with patch("app.music.mpv.subprocess.Popen") as m_popen, \
         patch("app.music.mpv.os.path.exists", return_value=True), \
         patch("app.music.mpv.open", create=True) as m_open:
        m_open.return_value = MagicMock()
        mpv = MpvController(sys.executable)
        assert mpv.start(taskkill=False, pipe_timeout=1.0, prop_timeout=0.3)
        args = m_popen.call_args[0][0]
        assert "--no-video" in args
        assert "--force-window=yes" not in args


# ---------- 互斥（双向暂停，不循环） ----------

class _FakeMusic:
    def __init__(self):
        self.paused = 0

    def pause(self):
        self.paused += 1


def test_movie_play_pauses_music():
    music = _FakeMusic()
    ctrl, mmpv = _make_ctrl(music=music)
    mmpv.is_running.return_value = False
    mmpv.start.return_value = True
    ok = ctrl.play({"vod_name": "X", "first_url": "https://u/i.m3u8"})
    assert ok is True
    assert music.paused == 1
    # 内嵌 S1：wid 参数化——无播放窗口注入时显式 wid=None（独立窗口现状，语义零变化）
    mmpv.start.assert_called_once_with(taskkill=False, wid=None)
    mmpv.loadfile.assert_called_once_with("https://u/i.m3u8")
    ctrl.close()


def test_movie_play_no_url_no_pause_music():
    music = _FakeMusic()
    ctrl, mmpv = _make_ctrl(music=music)
    assert ctrl.play({"vod_name": "X"}) is False
    assert ctrl.play("not a dict") is False
    assert music.paused == 0
    ctrl.close()


def test_movie_play_mpv_start_fail():
    music = _FakeMusic()
    ctrl, mmpv = _make_ctrl(music=music)
    mmpv.is_running.return_value = False
    mmpv.start.return_value = False
    assert ctrl.play({"vod_name": "X", "first_url": "https://u/i.m3u8"}) is False
    assert music.paused == 1
    ctrl.close()


def test_music_track_changed_pauses_movie():
    """音乐换歌（MUSIC_TRACK_CHANGED）→ 电影暂停（保留进度，事件解耦）。"""
    ctrl, mmpv = _make_ctrl()
    mmpv.is_running.return_value = False
    mmpv.start.return_value = True
    assert ctrl.play({"vod_name": "X", "first_url": "https://u/i.m3u8"})
    mmpv.is_running.return_value = True
    mmpv.pause.reset_mock()
    get_event_bus().publish(Event(EventType.MUSIC_TRACK_CHANGED, {"title": "x"}))
    mmpv.pause.assert_called_once()
    assert mmpv.stop.call_count == 0   # 暂停不是停止
    ctrl.close()


def test_music_play_event_pauses_movie():
    """音乐继续播放（MUSIC_PLAY）→ 电影暂停（双向对称的另一半）。"""
    ctrl, mmpv = _make_ctrl()
    mmpv.is_running.return_value = False
    mmpv.start.return_value = True
    assert ctrl.play({"vod_name": "X", "first_url": "https://u/i.m3u8"})
    mmpv.is_running.return_value = True
    mmpv.pause.reset_mock()
    get_event_bus().publish(Event(EventType.MUSIC_PLAY, {}))
    mmpv.pause.assert_called_once()
    assert mmpv.stop.call_count == 0
    ctrl.close()


def test_exclusive_no_loop():
    """music.pause 反向发布 MUSIC_PLAY 事件 → 电影暂停；不循环、不互相递归。"""
    bus = get_event_bus()

    class LoopMusic(_FakeMusic):
        def pause(self):
            super().pause()
            bus.publish(Event(EventType.MUSIC_PLAY, {}))

    music = LoopMusic()
    ctrl, mmpv = _make_ctrl(music=music)
    mmpv.is_running.return_value = False
    mmpv.start.return_value = True
    ok = ctrl.play({"vod_name": "X", "first_url": "https://u/i.m3u8"})
    assert ok is True
    assert music.paused == 1   # 电影 pause 不反向调 music.pause → 无循环
    assert ctrl._stopping is False
    ctrl.close()


# ---------- 面板渲染（offscreen） ----------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class _FakeMovie:
    def __init__(self):
        self.search_out = []
        self.play_ok = True
        self.stopped = 0

    def search(self, kw):
        return self.search_out

    def play(self, item, index=None):
        return self.play_ok

    def is_auto_next(self):
        return True

    def set_auto_next(self, enabled):
        pass

    def toggle_pause(self):
        pass

    def stop(self):
        self.stopped += 1

    def is_playing(self):
        return False

    def get_position(self):
        return 0.0

    def get_duration(self):
        return 0.0


def _mk_panel(qapp, fake):
    from app.ui.panels.movie_panel import MoviePanel
    panel = MoviePanel(movie=fake)
    return panel


def test_panel_search_done_renders(qapp):
    fake = _FakeMovie()
    fake.search_out = [
        {"vod_name": "流浪地球2", "vod_remarks": "HD", "first_url": "https://a/i.m3u8"},
        {"vod_name": "星际穿越", "vod_remarks": "", "first_url": "https://b/i.m3u8"},
    ]
    panel = _mk_panel(qapp, fake)
    try:
        panel._on_search_done(fake.search_out)
        assert panel.list_results.count() == 2
        assert "流浪地球2" in panel.list_results.item(0).text()
        assert "HD" in panel.list_results.item(0).text()
        assert panel.list_results.item(1).text() == "星际穿越"
        assert panel.lbl_status.text().startswith("找到")
    finally:
        panel._timer.stop()


def test_panel_search_empty(qapp):
    fake = _FakeMovie()
    panel = _mk_panel(qapp, fake)
    try:
        panel._on_search_done([])
        assert panel.list_results.count() == 0
        assert panel.lbl_status.text() == "没有找到匹配影片，请换个片名或切换片源后重试。"
    finally:
        panel._timer.stop()


def test_panel_search_failed(qapp):
    fake = _FakeMovie()
    panel = _mk_panel(qapp, fake)
    try:
        panel._on_search_failed("timeout")
        assert "搜索失败" in panel.lbl_status.text()
    finally:
        panel._timer.stop()


def test_panel_item_activated_plays(qapp):
    fake = _FakeMovie()
    panel = _mk_panel(qapp, fake)
    try:
        from PySide6.QtWidgets import QListWidgetItem
        it = {"vod_name": "流浪地球2", "first_url": "https://a/i.m3u8"}
        item = QListWidgetItem("流浪地球2")
        item.setData(0x0100, it)   # Qt.UserRole
        panel._on_item_activated(item)
        assert "播放中" in panel.lbl_now.text()
        assert panel.slider_pos.isEnabled()
    finally:
        panel._timer.stop()


def test_panel_item_activated_no_url(qapp):
    fake = _FakeMovie()
    fake.play_ok = False
    panel = _mk_panel(qapp, fake)
    try:
        from PySide6.QtWidgets import QListWidgetItem
        item = QListWidgetItem("X")
        item.setData(0x0100, {"vod_name": "X"})
        panel._on_item_activated(item)
        assert "无法播放" in panel.lbl_status.text()
    finally:
        panel._timer.stop()
