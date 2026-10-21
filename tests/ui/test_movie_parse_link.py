# -*- coding: utf-8 -*-
"""链接解析第一阶段：Worker + 面板入口 + 防重入（mock videodl，不碰网络/播放链路）。"""
import sys
from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.ui

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

sys.path.insert(0, ".")

from app.ui.dialogs.parse_link_dialog import ParseLinkDialog  # noqa: E402
from app.ui.panels.movie_panel import _ParseLinkWorker, MoviePanel  # noqa: E402

app = QApplication.instance() or QApplication([])


class _FakeInfo:
    download_url = "videodl_outputs/IQiyiVideoClient/123.m3u8"
    title = "正片｜电影"
    ext = "mp4"
    cover_url = "http://cover/x.avif"
    source = "IQiyiVideoClient"
    identifier = 123
    raw_data = {
        "highest_defns_original": "#EXTM3U\n#EXTINF:10,\n"
                                  "https://pcw-data.video.iqiyi.com/v1.ts?qd_k=abc\n",
        "referrer": "https://www.iqiyi.com/v_x.html",
    }


class _FakeClient:
    def parsefromurl(self, url):  # noqa: N802
        return [_FakeInfo()]


class _FakeClientNoPlaylist:
    """模拟真实情况：highest_defns_original 缺失，只有底层分段 URL（normal_ids）。"""

    def __init__(self, info):
        self._info = info
    def parsefromurl(self, url):  # noqa: N802
        return [self._info]


def _run_worker_collect(worker, *signals):
    """同步收集 worker 信号（QThread 环境外直接触发 run）。"""
    got = []
    for sig in signals:
        sig.connect(lambda *a: got.append(a))
    worker.run()
    return got


def test_worker_done_emits_m3u8():
    with patch("videodl.videodl.VideoClient", return_value=_FakeClient()):
        w = _ParseLinkWorker("https://www.iqiyi.com/v_x.html")
        got = _run_worker_collect(w, w.done, w.failed)
    assert got and got[0][0] == "正片｜电影"
    assert got[0][1].endswith(".m3u8")   # play_url
    assert got[0][2].startswith("http")  # display_url 是真实网络流


def test_worker_caches_real_m3u8_to_temp():
    """raw_data.highest_defns_original → %TEMP% 缓存，内容为真实网络清单。"""
    import os
    import tempfile
    with patch("videodl.videodl.VideoClient", return_value=_FakeClient()):
        w = _ParseLinkWorker("https://www.iqiyi.com/v_x.html")
        got = _run_worker_collect(w, w.done)
    play_url = got[0][1]
    assert os.path.isabs(play_url)
    assert os.path.dirname(play_url) == os.path.join(tempfile.gettempdir(), "lol_music_parse")
    with open(play_url, encoding="utf-8") as f:
        assert "https://pcw-data.video.iqiyi.com/v1.ts" in f.read()


def test_worker_fallback_builds_playlist_from_segments():
    """highest_defns_original 缺失时：从 normal_ids 底层分段 URL 自建真实网络清单。"""
    import os
    info = _FakeInfo()
    info.raw_data = {
        "normal_ids": [{
            "V": 123, "E": 1,
            "defns": {"shd": [{"ext": "ts", "urls": [
                "https://pcw-data.video.iqiyi.com/a.ts?qd_k=x",
                "https://pcw-data.video.iqiyi.com/b.ts?qd_k=y",
            ]}]},
        }],
    }
    with patch("videodl.videodl.VideoClient", return_value=_FakeClientNoPlaylist(info)):
        w = _ParseLinkWorker("https://www.iqiyi.com/v_x.html")
        got = _run_worker_collect(w, w.done)
    play_url = got[0][1]
    assert os.path.isabs(play_url) and play_url.endswith(".m3u8")
    assert got[0][2].startswith("https://pcw-data.video.iqiyi.com/a.ts")
    with open(play_url, encoding="utf-8") as f:
        text = f.read()
    assert "#EXTM3U" in text
    assert "b.ts" in text


def test_worker_passes_empty_sources_to_avoid_lib_bug():
    """必须传 allowed_video_sources=[]（库默认 None 崩溃）。"""
    with patch("videodl.videodl.VideoClient", return_value=_FakeClient()) as vc:
        w = _ParseLinkWorker("https://x")
        _run_worker_collect(w, w.done)
    assert vc.call_args.kwargs.get("allowed_video_sources") == []


def test_worker_failed_on_exception():
    boom = MagicMock()
    boom.parsefromurl.side_effect = RuntimeError("网络超时")
    with patch("videodl.videodl.VideoClient", return_value=boom):
        w = _ParseLinkWorker("https://x")
        got = _run_worker_collect(w, w.done, w.failed)
    assert got and got[0][0]  # failed 触发
    assert "网络超时" in got[0][0]


def test_worker_failed_on_empty_infos():
    empty = MagicMock()
    empty.parsefromurl.return_value = []
    with patch("videodl.videodl.VideoClient", return_value=empty):
        w = _ParseLinkWorker("https://x")
        got = _run_worker_collect(w, w.done, w.failed)
    assert got and got[0][0]  # failed 触发


def _make_panel():
    mv = MagicMock()
    mv._source_manager = None
    p = MoviePanel(mv)
    return p


def test_panel_has_parse_link_button():
    p = _make_panel()
    assert p.btn_parse_link is not None
    assert "链接解析" in p.btn_parse_link.text()


def _patch_dlg(dlg):
    """patch ParseLinkDialog 为带 Accepted=1 语义的替身（真实 QDialog.Accepted==1）。"""
    from PySide6.QtWidgets import QDialog
    FakeDlgCls = MagicMock(Accepted=QDialog.Accepted)
    FakeDlgCls.return_value = dlg
    dlg.exec.return_value = QDialog.Accepted
    dlg.third_party_api.return_value = ""  # 默认未填第三方接口
    return patch("app.ui.panels.movie_panel.ParseLinkDialog", FakeDlgCls)


def test_panel_empty_url_notifies():
    p = _make_panel()
    dlg = MagicMock()
    dlg.url.return_value = "   "
    with _patch_dlg(dlg), \
         patch("app.ui.panels.movie_panel.glass_info") as gi:
        p._on_parse_link()
    assert gi.called
    assert "链接为空" in gi.call_args[0][1]


def test_panel_starts_worker_on_valid_url():
    """有效链接 → 创建线程 + worker（worker 换成无副作用替身，不真跑网络）。"""
    p = _make_panel()
    dlg = MagicMock()
    dlg.url.return_value = "https://www.iqiyi.com/v_x.html"

    class _FakeWorker:
        def __init__(self, url):
            self.url = url
            self.done = MagicMock()
            self.failed = MagicMock()
        def moveToThread(self, t):
            pass
        def run(self):
            pass

    with _patch_dlg(dlg), \
         patch("app.ui.panels.movie_panel._ParseLinkWorker", _FakeWorker):
        p._on_parse_link()
    assert p._parse_thread is not None
    assert p._parse_worker is not None
    assert p._parse_worker.url == "https://www.iqiyi.com/v_x.html"
    p._parse_thread.quit()
    p._parse_thread.wait(2000)


def test_panel_rejects_second_parse_while_running():
    p = _make_panel()
    dlg = MagicMock()
    dlg.url.return_value = "https://x"
    busy = MagicMock()
    busy.isRunning.return_value = True
    p._parse_thread = busy
    with _patch_dlg(dlg), \
         patch("app.ui.panels.movie_panel.glass_info") as gi:
        p._on_parse_link()
    assert gi.called
    assert "解析中" in gi.call_args[0][1]


def test_panel_third_party_api_plays_directly():
    """填了第三方接口 → 拼「接口+链接」直接播放，不走 videodl worker。"""
    p = _make_panel()
    dlg = MagicMock()
    dlg.url.return_value = "https://www.iqiyi.com/v_x.html"
    with _patch_dlg(dlg):
        dlg.third_party_api.return_value = "https://jx.xxx.com/?url="
        with patch.object(p, "_play_parsed_url") as play:
            p._on_parse_link()
    play.assert_called_once()
    assert play.call_args[0][0] == "https://jx.xxx.com/?url=https://www.iqiyi.com/v_x.html"


def test_play_parsed_url_uses_existing_mpv():
    """直接调现有电影 mpv 实例（loadfile+play），不经过 movie_controller.play。"""
    p = _make_panel()
    mpv = MagicMock()
    mpv._process = MagicMock()
    mpv._process.poll.return_value = None  # 进程活着 → 复用
    p._movie.mpv = mpv
    p._play_parsed_url("https://cdn/x.m3u8", title="测试流")
    mpv.loadfile.assert_called_once_with("https://cdn/x.m3u8")
    mpv.play.assert_called_once()
    mpv.start.assert_not_called()


def test_play_parsed_url_starts_if_not_alive():
    """mpv 未启动 → 先 start（无 wid、短超时）再 loadfile。"""
    p = _make_panel()
    mpv = MagicMock()
    mpv._process = None  # 未启动
    mpv.start.return_value = True
    p._movie.mpv = mpv
    p._play_parsed_url("https://cdn/y.m3u8")
    mpv.start.assert_called_once()
    assert mpv.start.call_args.kwargs.get("wid") is None
    mpv.loadfile.assert_called_once_with("https://cdn/y.m3u8")


def test_parse_done_play_button_triggers_play():
    """结果弹窗点「▶ 立即播放」→ _play_parsed_url。"""
    p = _make_panel()
    box = MagicMock()
    box._result = "▶ 立即播放"
    with patch("app.ui.panels.movie_panel._GlassMsgBox", return_value=box), \
         patch.object(p, "_play_parsed_url") as play:
        p._on_parse_done("标题", "https://cdn/z.m3u8", "https://pcw-data.video.iqiyi.com/v1.ts")
    play.assert_called_once()
    assert play.call_args[0][0] == "https://cdn/z.m3u8"


def test_parse_done_dismiss_does_not_play():
    p = _make_panel()
    box = MagicMock()
    box._result = "确定"
    with patch("app.ui.panels.movie_panel._GlassMsgBox", return_value=box), \
         patch.object(p, "_play_parsed_url") as play:
        p._on_parse_done("标题", "https://cdn/z.m3u8", "http://d")
    play.assert_not_called()
