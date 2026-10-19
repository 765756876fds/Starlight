# -*- coding: utf-8 -*-
"""链接解析第一阶段：Worker + 面板入口 + 防重入（mock videodl，不碰网络/播放链路）。"""
import sys
from unittest.mock import MagicMock, patch

import pytest
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


class _FakeClient:
    def parsefromurl(self, url):  # noqa: N802
        return [_FakeInfo()]


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
    assert got[0][1].endswith(".m3u8")


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
