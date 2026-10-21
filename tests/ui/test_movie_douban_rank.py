# -*- coding: utf-8 -*-
"""批 2 测试：电影榜单（豆瓣 douban_rank 模块 + 面板榜单视图 + 海报 Referer + 降级）。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.ui


from PySide6.QtCore import QThread
from PySide6.QtWidgets import QApplication, QListWidget, QListWidgetItem

_app = QApplication.instance() or QApplication([])

from app.movie import douban_rank
from app.ui.panels import movie_panel as mp
from app.ui.panels.movie_panel import MoviePanel

import requests


# ---------- 豆瓣模块 ----------

def _resp(status=200, payload=None, exc=None):
    m = MagicMock()
    if exc is not None:
        m.get.side_effect = exc
        return m
    m.get.return_value.status_code = status
    if payload is not None:
        m.get.return_value.json.return_value = payload
    return m


def test_get_tags_parses():
    with patch.object(requests, "get") as m_get:
        m_get.return_value.status_code = 200
        m_get.return_value.json.return_value = {"tags": ["热门", "最新", "豆瓣高分"]}
        assert douban_rank.get_tags() == ["热门", "最新", "豆瓣高分"]
        args, kwargs = m_get.call_args
        assert "search_tags" in args[0]
        assert kwargs["headers"]["User-Agent"]
        assert kwargs["headers"]["Referer"].startswith("https://movie.douban.com/")


def test_get_tags_non_200_returns_empty():
    with patch.object(requests, "get") as m_get:
        m_get.return_value.status_code = 500
        assert douban_rank.get_tags() == []


def test_get_tags_error_returns_empty():
    with patch.object(requests, "get", side_effect=OSError("boom")):
        assert douban_rank.get_tags() == []


def test_get_subjects_parses():
    subs = [{"title": "肖申克的救赎", "rate": "9.7", "cover": "https://img/c.jpg",
             "url": "https://movie.douban.com/subject/1292052/", "id": "1292052"},
            {"title": "希望", "rate": "5.6", "cover": "", "url": "", "id": "36179101"}]
    with patch.object(requests, "get") as m_get:
        m_get.return_value.status_code = 200
        m_get.return_value.json.return_value = {"subjects": subs}
        out = douban_rank.get_subjects("热门")
        assert out[0] == {"title": "肖申克的救赎", "rate": "9.7",
                          "cover": "https://img/c.jpg",
                          "url": "https://movie.douban.com/subject/1292052/",
                          "id": "1292052"}
        assert out[1]["cover"] == "" and out[1]["id"] == "36179101"
        _, kwargs = m_get.call_args
        assert kwargs["params"] == {"type": "movie", "tag": "热门", "sort": "recommend",
                                    "page_limit": 20, "page_start": 0}
        assert kwargs["headers"]["Referer"]


def test_get_subjects_missing_key_returns_empty():
    with patch.object(requests, "get") as m_get:
        m_get.return_value.status_code = 200
        m_get.return_value.json.return_value = {}
        assert douban_rank.get_subjects("热门") == []


def test_get_subjects_error_returns_empty():
    with patch.object(requests, "get", side_effect=requests.Timeout("slow")):
        assert douban_rank.get_subjects("热门") == []


def test_get_subjects_skips_non_dict():
    with patch.object(requests, "get") as m_get:
        m_get.return_value.status_code = 200
        m_get.return_value.json.return_value = {"subjects": [None, "x", {}]}
        out = douban_rank.get_subjects("热门")
        assert len(out) == 1 and out[0]["title"] == ""


# ---------- 面板：榜单视图 ----------

class _FakeMovie:
    def __init__(self):
        self.search_calls = []
        self.play_calls = []
        self.close_calls = 0

    def close(self):
        self.close_calls += 1

    def search(self, kw):
        self.search_calls.append(kw)
        return [{"vod_id": 1, "vod_name": kw, "vod_score": "9.7"}]

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


def _panel():
    movie = _FakeMovie()
    p = MoviePanel(movie=movie)
    return p, movie


def _fake_qthread(monkeypatch):
    """真实 QThread 子类替身：moveToThread 类型校验通过，start 不真起线程。"""
    class _FakeQThread(QThread):
        def __init__(self):
            super().__init__()
            self.running = False
            self.started_calls = 0

        def isRunning(self):
            return self.running

        def start(self, *a, **k):
            self.started_calls += 1
            self.running = True

        def quit(self):
            self.running = False

    monkeypatch.setattr("app.ui.panels.movie_panel.QThread", _FakeQThread)
    return _FakeQThread


_SUBS = [
    {"title": "肖申克的救赎", "rate": "9.7", "cover": "https://img/c1.jpg",
     "url": "https://movie.douban.com/subject/1292052/", "id": "1292052"},
    {"title": "希望", "rate": "5.6", "cover": "", "url": "", "id": "36179101"},
]


def test_panel_has_rank_view():
    p, _ = _panel()
    assert p.btn_rank.text() == "榜单"
    assert p.stack_results.indexOf(p.page_rank) == 2
    assert p.list_rank_tags.count() == 0
    assert isinstance(p.list_rank, QListWidget)


def test_enter_rank_view_shows_page_and_loads_tags():
    p, _ = _panel()
    with patch.object(p, "_load_rank_tags") as m_load:
        p._enter_rank_view()
        assert p.stack_results.currentWidget() is p.page_rank
        m_load.assert_called_once()
        # 二次进入：标签已加载 → 不再重拉
        with patch.object(p, "_load_rank_tags") as m_load2:
            p.list_rank_tags.addItem("热门")
            p._enter_rank_view()
            m_load2.assert_not_called()


def test_rank_tags_done_renders_and_loads_first():
    p, _ = _panel()
    with patch.object(p, "_load_rank_subjects") as m_load:
        p._on_rank_tags_done(["热门", "最新"])
        assert p.list_rank_tags.count() == 2
        assert p.list_rank_tags.item(0).data(0x0100) == "热门"
        assert p.list_rank_tags.currentRow() == 0
        # 时序修复：默认"热门"延后一拍（旧线程 quit 排队期防重入不丢加载）
        assert m_load.call_count == 0
        _app.processEvents()
        m_load.assert_called_once_with("热门")


def test_rank_tags_done_empty():
    p, _ = _panel()
    p._on_rank_tags_done([])
    assert p.list_rank_tags.count() == 0
    assert "为空" in p.lbl_status.text()


def test_rank_tags_failed_degrades():
    p, _ = _panel()
    p._on_rank_tags_failed("网络错误")
    assert "榜单加载失败" in p.lbl_status.text()


def test_load_rank_subjects_renders_grid(tmp_path):
    p, _ = _panel()
    mp._posters_cache.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):  # 隔离真实海报缓存（实机已下载）
        with patch.object(p, "_start_db_poster_jobs") as m_jobs:
            p._on_rank_subjects_done(list(_SUBS), "热门")
            assert p.list_rank.count() == 2
            # db_key 存 item 数据（海报 done 匹配用）
            it = p.list_rank.item(0)
            assert it.data(0x0100)["db_key"] == "db1292052"
            assert "评分 9.7" in it.toolTip()
            # 只有带 cover 的条目提交下载
            m_jobs.assert_called_once()
            assert m_jobs.call_args[0][0] == [("db1292052", "https://img/c1.jpg")]
            assert "榜单「热门」2 部" in p.lbl_status.text()
    mp._posters_cache.clear()


def test_rank_subjects_empty_message():
    p, _ = _panel()
    p._on_rank_subjects_done([], "冷门佳片")
    assert p.list_rank.count() == 0
    assert "榜单「冷门佳片」为空" in p.lbl_status.text()


def test_rank_subjects_failed_degrades():
    p, _ = _panel()
    p._on_rank_subjects_failed("超时")
    assert "榜单加载失败" in p.lbl_status.text()


def test_rank_tag_click_loads_when_changed():
    p, _ = _panel()
    p._rank_tag = "热门"
    with patch.object(p, "_load_rank_subjects") as m_load:
        item = QListWidgetItem("最新")
        item.setData(0x0100, "最新")
        p.list_rank_tags.addItem(item)
        p._on_rank_tag_clicked(item)
        m_load.assert_called_once_with("最新")
        # 同标签重复点 → 不重拉
        item2 = QListWidgetItem("热门")
        item2.setData(0x0100, "热门")
        p.list_rank_tags.addItem(item2)
        p._on_rank_tag_clicked(item2)
        assert m_load.call_count == 1


def test_rank_item_activated_searches_cms_not_autoplay():
    p, movie = _panel()
    with patch.object(p, "_start_db_poster_jobs"):
        p._on_rank_subjects_done(list(_SUBS), "热门")
    with patch.object(p, "_do_search") as m_search:
        p._on_rank_item_activated(p.list_rank.item(0))
        assert p.edit_kw.text() == "肖申克的救赎"
        assert p.stack_results.currentWidget() is p.list_posters
        m_search.assert_called_once()
        assert movie.play_calls == []  # 不自动播


def test_rank_thread_reentry_ignored():
    """榜单线程运行中 → 不新建不覆盖（QThread 崩溃教训）。"""
    p, _ = _panel()
    with patch.object(mp, "_RankTagsWorker") as m_w:
        with patch.object(mp, "_RankSubjectsWorker") as m_w2:
            with patch.object(p, "_rank_thread") as m_thread:
                m_thread.isRunning.return_value = True
                p._load_rank_tags()
                m_w.assert_not_called()
                p._load_rank_subjects("热门")
                m_w2.assert_not_called()


def test_rank_thread_starts_once(monkeypatch):
    cls = _fake_qthread(monkeypatch)
    p, _ = _panel()
    p._load_rank_tags()
    assert p._rank_thread.started_calls == 1


# ---------- 榜单海报（带 Referer 下载 + done 更新榜单项） ----------

def _fake_jpg():
    """有效 JPEG bytes（渐变 400×560 >5000B，通过 worker 内容校验）。"""
    from PySide6.QtCore import QBuffer
    from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPixmap
    buf = QBuffer()
    buf.open(QBuffer.WriteOnly)
    pix = QPixmap(400, 560)
    g = QLinearGradient(0, 0, 400, 560)
    g.setColorAt(0, QColor(200, 30, 30))
    g.setColorAt(1, QColor(30, 200, 60))
    painter = QPainter(pix)
    painter.fillRect(pix.rect(), g)
    painter.end()
    pix.save(buf, "JPG")
    return bytes(buf.data())


def _fake_html():
    """豆瓣反爬页：HTTP 200 但 text/html ~1KB 非 JPEG。"""
    return b"<script>function x(){}</script>" * 60


def test_db_poster_worker_sends_referer_and_writes(tmp_path):
    """有效 JPEG → 缩略（圆角）写盘 + Referer 头正确。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get") as m_get:
            m_get.return_value.status_code = 200
            m_get.return_value.content = _fake_jpg()
            w = _DbPosterBatchWorker([("db1292052", "https://img/c1.jpg")])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert list(got.keys()) == ["db1292052"]
            saved = tmp_path / "db1292052.jpg"
            assert saved.exists() and saved.stat().st_size > 1000
            _, kwargs = m_get.call_args
            assert kwargs["headers"]["Referer"].startswith("https://movie.douban.com/")
            assert kwargs["headers"]["User-Agent"]


def test_db_poster_worker_rejects_html_content(tmp_path):
    """豆瓣无海报条目返回 text/html ~990B 反爬页（非图）→ done 空 + 记失败集 + 不写盘。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker, _db_poster_failed
    _db_poster_failed.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get") as m_get:
            m_get.return_value.status_code = 200
            m_get.return_value.content = _fake_html()
            w = _DbPosterBatchWorker([("db1292052", "https://img/c1.jpg")])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert got == {"db1292052": ""}
            assert "https://img/c1.jpg" in _db_poster_failed  # 会话内不再重试
            assert not (tmp_path / "db1292052.jpg").exists()
    _db_poster_failed.clear()


def test_poster_candidates_original_domain_first():
    """多域候选：原域在首位，其余 img1..img9 按序；非 doubanio 域原样单候选。"""
    from app.ui.panels.movie_panel import _poster_candidates
    url = "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2936309806.jpg"
    cands = _poster_candidates(url)
    assert len(cands) == 9
    assert cands[0].startswith("https://img9.doubanio.com/")   # 原域优先（多数直达成功）
    assert cands[1].startswith("https://img1.doubanio.com/")
    assert cands[2].startswith("https://img2.doubanio.com/")
    assert len(set(cands)) == 9                                 # 9 域互不相同
    assert _poster_candidates("https://img/c1.jpg") == ["https://img/c1.jpg"]


def test_db_poster_worker_domain_fallback(tmp_path):
    """主域（img9）反爬页 → 换域（img1）成功（取证 5/5 有效路径）+ 不记失败集。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker, _db_poster_failed
    url = "https://img9.doubanio.com/view/photo/s_ratio_poster/public/p2936309806.jpg"
    _db_poster_failed.clear()

    def side_effect(u, **kw):
        resp = requests.get.return_value
        resp.status_code = 200
        resp.content = _fake_html() if "img9." in u else _fake_jpg()
        return resp

    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get", side_effect=side_effect) as m_get:
            w = _DbPosterBatchWorker([("db1292052", url)])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert (tmp_path / "db1292052.jpg").exists()
            assert any("img1.doubanio.com" in c[0][0] for c in m_get.call_args_list)
            assert url not in _db_poster_failed  # 换域成功 → 不记失败
    _db_poster_failed.clear()


def test_db_poster_worker_all_domains_fail(tmp_path):
    """9 域全反爬页 → done 空 + 记失败集（占位兜底，不崩）。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker, _db_poster_failed
    url = "https://img9.doubanio.com/x.jpg"
    _db_poster_failed.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get") as m_get:
            m_get.return_value.status_code = 200
            m_get.return_value.content = _fake_html()
            w = _DbPosterBatchWorker([("db1292052", url)])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert got == {"db1292052": ""}
            assert url in _db_poster_failed
            assert not (tmp_path / "db1292052.jpg").exists()
            assert m_get.call_count == 9  # 9 域全试后放弃
    _db_poster_failed.clear()


def test_db_poster_worker_main_domain_success_no_fallback(tmp_path):
    """主域直接成功 → 只请求一次（不换域，不拖慢）。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get") as m_get:
            m_get.return_value.status_code = 200
            m_get.return_value.content = _fake_jpg()
            w = _DbPosterBatchWorker([("db1292052", "https://img9.doubanio.com/x.jpg")])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert (tmp_path / "db1292052.jpg").exists()
            assert m_get.call_count == 1
    mp._posters_cache.clear()


def test_db_poster_worker_uses_concurrency():
    """并发下载（对齐音乐下载器）：ThreadPoolExecutor max_workers=4。"""
    from app.ui.panels.movie_panel import _DbPosterBatchWorker
    with patch("app.ui.panels.movie_panel.ThreadPoolExecutor") as m_ex:
        m_ex.return_value.__enter__.return_value = m_ex.return_value
        with patch.object(requests, "get", side_effect=OSError("x")):
            w = _DbPosterBatchWorker([("db1", "u1"), ("db2", "u2")])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert got  # 逐张 done 仍发射
        assert m_ex.call_args.kwargs["max_workers"] == 4


def test_make_thumb_pixmap_rounds_and_scales():
    """缩略：SmoothTransformation 缩放 + 圆角输出（对齐爱奇艺海报样式）。"""
    from app.ui.panels.movie_panel import _make_thumb_pixmap
    from PySide6.QtGui import QColor, QPixmap
    src = QPixmap(540, 770)
    src.fill(QColor(200, 30, 30))
    out = _make_thumb_pixmap(src)
    assert not out.isNull()
    assert out.width() == 140 and out.height() == 210  # _POSTER_SIZE
    # 透明角（圆角裁剪生效：角部为透明）
    assert out.toImage().pixelColor(1, 1).alpha() == 0
    assert out.toImage().pixelColor(70, 105).alpha() == 255


def test_db_poster_worker_failure_emits_empty(tmp_path):
    from app.ui.panels.movie_panel import _DbPosterBatchWorker
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(requests, "get", side_effect=OSError("down")):
            w = _DbPosterBatchWorker([("db1", "https://img/x.jpg")])
            got = {}
            w.done.connect(lambda k, path: got.update({k: path}))
            w.run()
            assert got == {"db1": ""}


def test_poster_done_updates_rank_item(tmp_path):
    """_on_poster_done 按 db_key 更新榜单项 icon（对齐搜索海报墙）。"""
    from PySide6.QtGui import QPixmap
    p, _ = _panel()
    with patch.object(p, "_start_db_poster_jobs"):
        p._on_rank_subjects_done(list(_SUBS), "热门")
    fake = tmp_path / "db1292052.jpg"
    QPixmap(20, 30).save(str(fake))
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        p._on_poster_done("db1292052", str(fake))
        assert not p.list_rank.item(0).icon().isNull()


# ---------- QThread 崩溃修复：海报下载防重入 + 补偿 + 退出清理 ----------

def test_start_poster_jobs_reentry_pending(monkeypatch):
    """搜索海报下载：旧线程运行中 → 不新建不覆盖，记最新一批待处理。"""
    cls = _fake_qthread(monkeypatch)
    p, _ = _panel()
    t = cls()
    t.running = True
    p._poster_thread = t
    p._start_poster_jobs([("1", "https://x/1.jpg")])
    assert p._pending_poster_jobs == [("1", "https://x/1.jpg")]
    assert t.started_calls == 0
    # 快速连点 A→B：pending 被最新覆盖
    p._start_poster_jobs([("2", "https://y/2.jpg")])
    assert p._pending_poster_jobs == [("2", "https://y/2.jpg")]
    assert t.started_calls == 0


def test_start_poster_jobs_starts_when_idle(monkeypatch):
    cls = _fake_qthread(monkeypatch)
    p, _ = _panel()
    p._start_poster_jobs([("1", "https://x/1.jpg")])
    assert p._poster_thread.started_calls == 1
    assert p._pending_poster_jobs is None


def test_poster_batch_finished_compensates():
    """旧批次全部完成后 → 自动补下载最新一批（延后一拍，不丢）。"""
    p, _ = _panel()
    p._pending_poster_jobs = [("2", "https://y/2.jpg")]
    calls = []
    p._start_poster_jobs = lambda jobs: calls.append(jobs)
    p._on_poster_batch_finished()
    assert p._pending_poster_jobs is None
    assert calls == []  # 延后一拍
    _app.processEvents()
    assert calls == [[("2", "https://y/2.jpg")]]


def test_start_db_poster_jobs_reentry_pending(monkeypatch):
    """榜单海报下载：旧线程运行中 → 不新建不覆盖，记最新一批待处理。"""
    cls = _fake_qthread(monkeypatch)
    p, _ = _panel()
    t = cls()
    t.running = True
    p._rank_db_thread = t
    p._start_db_poster_jobs([("db1", "https://img/x.jpg")])
    assert p._pending_db_jobs == [("db1", "https://img/x.jpg")]
    assert t.started_calls == 0


def test_start_db_poster_jobs_starts_when_idle(monkeypatch):
    cls = _fake_qthread(monkeypatch)
    p, _ = _panel()
    p._start_db_poster_jobs([("db1", "https://img/x.jpg")])
    assert p._rank_db_thread.started_calls == 1
    assert p._pending_db_jobs is None


def test_db_poster_batch_finished_compensates():
    p, _ = _panel()
    p._pending_db_jobs = [("db2", "https://img/y.jpg")]
    calls = []
    p._start_db_poster_jobs = lambda jobs: calls.append(jobs)
    p._on_db_poster_batch_finished()
    assert p._pending_db_jobs is None
    assert calls == []
    _app.processEvents()
    assert calls == [[("db2", "https://img/y.jpg")]]


def test_on_app_quit_shuts_down_threads(monkeypatch):
    """退出清理：cancel 全部 worker + quit 运行中线程（有界 wait）+ 最后关 mpv。"""
    cls = _fake_qthread(monkeypatch)
    p, movie = _panel()
    ts = [cls(), cls(), cls(), cls()]
    for t in ts:
        t.running = True
    p._thread, p._rank_thread, p._poster_thread, p._rank_db_thread = ts

    class _W:
        def __init__(self):
            self.cancelled = False

        def cancel(self):
            self.cancelled = True

    ws = [_W(), _W(), _W(), _W()]
    p._worker, p._rank_worker, p._poster_worker, p._rank_db_worker = ws
    p._on_app_quit()
    assert all(not t.running for t in ts)          # 全部 quit
    assert all(w.cancelled for w in ws)            # 全部 cancel
    assert movie.close_calls == 1                   # 清理后才关 mpv


def test_on_app_quit_no_threads_no_crash():
    """无线程/无 worker 时退出清理不抛（FakeMovie 无 cancel 属性）。"""
    p, movie = _panel()
    p._thread = None
    p._rank_thread = None
    p._poster_thread = None
    p._rank_db_thread = None
    p._on_app_quit()
    assert movie.close_calls == 1


def test_poster_worker_cancel_stops_loop():
    """cancel 置位 → run 不下载任何一张。"""
    from app.ui.panels.movie_panel import _PosterBatchWorker
    with patch.object(requests, "get") as m_get:
        w = _PosterBatchWorker([("1", "https://x/1.jpg"), ("2", "https://y/2.jpg")])
        w.cancel()
        w.run()
        m_get.assert_not_called()


def test_db_poster_worker_cancel_stops_loop():
    from app.ui.panels.movie_panel import _DbPosterBatchWorker
    with patch.object(requests, "get") as m_get:
        w = _DbPosterBatchWorker([("db1", "https://img/x.jpg")])
        w.cancel()
        w.run()
        m_get.assert_not_called()


def test_poster_worker_finished_emitted():
    """整批结束 emit finished（面板用它补偿待处理批次）。"""
    from app.ui.panels.movie_panel import _PosterBatchWorker
    with patch.object(requests, "get", side_effect=OSError("x")):
        w = _PosterBatchWorker([("1", "https://x/1.jpg"), ("2", "https://y/2.jpg")])
        n = [0]
        w.finished.connect(lambda: n.__setitem__(0, n[0] + 1))
        w.run()
        assert n[0] == 1


# ---------- 海报优化：片名/评分数据 + delegate + 失败集跳过 ----------

def test_rank_items_carry_title_and_rate(tmp_path):
    """榜单项 UserRole 带 title/rate（delegate 绘制片名大字 + 评分小字）。"""
    from app.ui.panels.movie_panel import _RankItemDelegate
    p, _ = _panel()
    mp._posters_cache.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(p, "_start_db_poster_jobs"):
            p._on_rank_subjects_done(list(_SUBS), "热门")
        assert isinstance(p.list_rank.itemDelegate(), _RankItemDelegate)
        data = p.list_rank.item(0).data(0x0100)
        assert data["title"] == "肖申克的救赎"
        assert data["rate"] == "9.7"
        assert not p.list_rank.item(0).icon().isNull()  # thumb 化占位 icon
    mp._posters_cache.clear()


def test_rank_skips_failed_cover_jobs(tmp_path):
    """无效海报 URL（已记 _db_poster_failed）→ 渲染不再提交下载（不重复拉无效内容）。"""
    from app.ui.panels.movie_panel import _db_poster_failed
    p, _ = _panel()
    mp._posters_cache.clear()
    _db_poster_failed.add("https://img/c1.jpg")
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(p, "_start_db_poster_jobs") as m_jobs:
            p._on_rank_subjects_done(list(_SUBS), "热门")
            m_jobs.assert_not_called()
    _db_poster_failed.clear()
    mp._posters_cache.clear()


def test_rank_delegate_paint_direct_call(tmp_path):
    """delegate.paint() 直接调用不崩——真实渲染路径，offscreen UI 流程触发不到
    （QStyledItemDelegate.paint 只在真实绘制时执行；缺 QRect 等 import 只能靠直调暴露）。"""
    from PySide6.QtCore import QRect, Qt
    from PySide6.QtGui import QPainter, QPixmap
    from PySide6.QtWidgets import QStyle, QStyleOptionViewItem
    from app.ui.panels.movie_panel import _RankItemDelegate
    p, _ = _panel()
    mp._posters_cache.clear()
    with patch.object(mp, "_POSTER_DIR", new=tmp_path):
        with patch.object(p, "_start_db_poster_jobs"):
            p._on_rank_subjects_done(list(_SUBS), "热门")
    d = p.list_rank.itemDelegate()
    assert isinstance(d, _RankItemDelegate)
    it = p.list_rank.item(0)
    opt = QStyleOptionViewItem()
    opt.rect = QRect(0, 0, 170, 250)
    opt.state = QStyle.State_Selected | QStyle.State_MouseOver
    canvas = QPixmap(170, 250)
    canvas.fill(Qt.transparent)
    pnt = QPainter(canvas)
    try:
        d.paint(pnt, opt, it)  # 实机渲染路径：不抛 = import 齐全
    finally:
        pnt.end()
    mp._posters_cache.clear()
