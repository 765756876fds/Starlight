"""音乐榜单：bridge 协议静态断言 + js_source/manager/controller 封装 + 榜单 UI。

覆盖：
- bridge.js/mf_loader.js 暴露 getTopLists/getTopListDetail（插件自报能力）
- js_source 解析榜单分组/详情（→ OnlineTrack）
- manager 聚合/路由/失败降级
- controller 转发/兜底
- UI：榜单 tab 渲染、点歌走 play_online、降级不崩
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 必须在 import PySide6 前

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.music.online.sources.js_source import JSMusicSource
from app.music.online.models import OnlineTrack


# ---------- bridge.js / mf_loader.js 协议暴露（静态断言） ----------

def test_bridge_js_exposes_rank_protocol():
    base = Path(__file__).resolve().parents[1] / "app/music/online/js_bridge"
    bridge = (base / "bridge.js").read_text(encoding="utf-8")
    loader = (base / "lib/mf_loader.js").read_text(encoding="utf-8")
    assert "case 'getTopLists'" in bridge
    assert "case 'getTopListDetail'" in bridge
    assert "callGetTopLists" in loader
    assert "callGetTopListDetail" in loader
    assert "getTopLists: mod.getTopLists" in loader
    assert "getTopListDetail: mod.getTopListDetail" in loader
    # 插件自报能力：bridge 按 typeof 跳过无能力的插件，不硬编码平台
    assert "typeof p.getTopLists !== 'function'" in bridge
    assert "typeof plugin.getTopLists !== 'function'" in loader


# ---------- js_source 封装 ----------

def _src(monkeypatch, fake_call):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = None
    s._lock = threading.Lock()
    s._pending = {}
    s._sources = []
    monkeypatch.setattr(s, "_call", fake_call)
    return s


def test_js_get_top_lists_parses_groups(monkeypatch):
    def fake_call(method, params, timeout=15.0):
        assert method == "getTopLists"
        return [{"source": "酷我音乐", "groups": [
            {"title": "酷我热歌榜", "lists": [{"id": "93", "title": "酷我热歌榜", "cover_img": ""}]},
        ]}]
    out = _src(monkeypatch, fake_call).get_top_lists()
    assert out == {"酷我音乐": [
        {"title": "酷我热歌榜", "lists": [{"id": "93", "title": "酷我热歌榜", "cover_img": ""}]}]}


def test_js_get_top_lists_empty_on_none(monkeypatch):
    out = _src(monkeypatch, lambda m, p, timeout=15.0: None).get_top_lists()
    assert out == {}


def test_js_get_top_list_detail_parses_tracks(monkeypatch):
    def fake_call(method, params, timeout=15.0):
        assert method == "getTopListDetail"
        assert params == {"source": "酷我音乐", "list_id": "93", "page": 1}
        return {"is_end": False, "tracks": [
            {"source_name": "酷我音乐", "track_id": "1", "title": "A", "artist": "B",
             "album": "", "duration_seconds": 200, "extra": {"_raw": {}}},
        ]}
    out = _src(monkeypatch, fake_call).get_top_list_detail("酷我音乐", "93")
    assert out["is_end"] is False
    assert len(out["tracks"]) == 1
    assert out["tracks"][0].title == "A"
    assert out["tracks"][0].source_name == "酷我音乐"


def test_js_get_top_list_detail_empty_args(monkeypatch):
    s = _src(monkeypatch, lambda m, p, timeout=15.0: None)
    assert s.get_top_list_detail("", "93") is None
    assert s.get_top_list_detail("酷我音乐", "") is None


def test_js_get_top_list_detail_none_on_call_fail(monkeypatch):
    s = _src(monkeypatch, lambda m, p, timeout=15.0: None)
    assert s.get_top_list_detail("酷我音乐", "93") is None


# ---------- manager 聚合/路由 ----------

def _manager():
    from app.music.online.manager import OnlineSourceManager
    return OnlineSourceManager(max_workers=2)


def test_manager_get_top_lists_aggregates():
    m = _manager()
    src = MagicMock()
    src.get_top_lists.return_value = {"酷我音乐": [{"title": "热歌", "lists": []}]}
    m.register(src)
    out = m.get_top_lists()
    assert out["酷我音乐"][0]["title"] == "热歌"
    src.get_top_lists.assert_called_once()


def test_manager_get_top_lists_skips_capability_less():
    m = _manager()
    src = MagicMock()
    del src.get_top_lists
    m.register(src)
    assert m.get_top_lists() == {}


def test_manager_get_top_lists_failure_recorded():
    m = _manager()
    src = MagicMock()
    src.get_top_lists.side_effect = RuntimeError("boom")
    m.register(src)
    assert m.get_top_lists() == {}
    assert len(m._errors) >= 1


def test_manager_get_top_list_detail_routes():
    m = _manager()
    src = MagicMock()
    src.get_top_list_detail.return_value = {"is_end": True, "tracks": []}
    m.register(src)
    out = m.get_top_list_detail("酷我音乐", "93")
    assert out == {"is_end": True, "tracks": []}
    src.get_top_list_detail.assert_called_once_with("酷我音乐", "93", 1)


def test_manager_get_top_list_detail_none_when_missing():
    m = _manager()
    src = MagicMock()
    del src.get_top_list_detail
    m.register(src)
    assert m.get_top_list_detail("x", "1") is None


# ---------- controller 转发 ----------

def _ctrl():
    from app.music.controller import MusicController
    c = MusicController.__new__(MusicController)
    c.online_manager = None
    return c


def test_controller_get_top_lists_no_manager():
    c = _ctrl()
    assert c.get_top_lists() == {}
    assert c.get_top_list_detail("x", "1") is None


def test_controller_get_top_lists_forward():
    c = _ctrl()
    m = MagicMock()
    m.get_top_lists.return_value = {"kw": []}
    m.get_top_list_detail.return_value = {"is_end": True, "tracks": []}
    c.online_manager = m
    assert c.get_top_lists() == {"kw": []}
    assert c.get_top_list_detail("kw", "1")["is_end"] is True
    m.get_top_list_detail.assert_called_once_with("kw", "1", 1)


def test_controller_get_top_lists_failure_safe():
    c = _ctrl()
    m = MagicMock()
    m.get_top_lists.side_effect = RuntimeError("x")
    m.get_top_list_detail.side_effect = RuntimeError("x")
    c.online_manager = m
    assert c.get_top_lists() == {}
    assert c.get_top_list_detail("kw", "1") is None


# ---------- UI 榜单 tab ----------

@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _panel():
    from app.ui.panels.music_panel import MusicPanel
    fake = MagicMock()
    fake.get_volume.return_value = 50
    fake.get_online_sources.return_value = []
    return MusicPanel(music=fake)


def test_rank_tab_exists(qapp):
    p = _panel()
    assert p.tabs.count() >= 5
    assert p.tabs.tabText(4) == "榜单"


def test_rank_lists_done_renders_sources_and_lists(qapp, monkeypatch):
    from PySide6.QtCore import Qt
    p = _panel()
    # _on_rank_lists_done 内部 setCurrentRow(0) 会级联触发 _on_rank_list_selected
    # → _load_rank_detail 起真 QThread（worker 把 MagicMock 经信号 emit 到 Qt，
    # 残留线程在进程退出时崩溃 0xC0000409）。测试里 patch 掉详情加载。
    monkeypatch.setattr(p, "_load_rank_detail", lambda *a, **k: None)
    p._on_rank_lists_done({"酷我音乐": [{"title": "热歌", "lists": [
        {"id": "93", "title": "热歌榜", "cover_img": ""}]}]})
    assert p.list_rank_sources.count() == 1
    assert p.list_rank_sources.item(0).text() == "酷我音乐"
    p.list_rank_sources.setCurrentRow(0)
    assert p.list_rank_lists.count() == 1
    assert "热歌" in p.list_rank_lists.item(0).text()
    assert p.list_rank_lists.item(0).data(Qt.UserRole)["list_id"] == "93"


def test_rank_lists_done_empty_degrades(qapp):
    p = _panel()
    p._on_rank_lists_done({})
    assert p.list_rank_sources.count() == 0
    assert p.list_rank_lists.count() == 0
    assert "暂无平台提供榜单" in p.lbl_rank_status.text()


def test_rank_source_no_lists_degrades(qapp):
    p = _panel()
    p._rank_groups = {"酷我音乐": []}
    p.list_rank_sources.addItem("酷我音乐")
    p.list_rank_sources.setCurrentRow(0)
    assert p.list_rank_lists.count() == 0
    assert "无榜单" in p.lbl_rank_status.text()


def test_rank_list_selected_triggers_detail(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    p = _panel()
    item = QListWidgetItem("热歌榜")
    item.setData(Qt.UserRole, {"source": "酷我音乐", "list_id": "93", "title": "热歌榜"})
    p.list_rank_lists.addItem(item)
    called = {}
    p._load_rank_detail = lambda s, lid, t: called.update(source=s, lid=lid, t=t)
    p._on_rank_list_selected(item, None)
    assert called == {"source": "酷我音乐", "lid": "93", "t": "热歌榜"}


def test_rank_detail_done_renders_ranked_songs(qapp):
    p = _panel()
    tracks = [
        OnlineTrack(source_name="酷我音乐", track_id="1", title="A", artist="B"),
        OnlineTrack(source_name="酷我音乐", track_id="2", title="C", artist="D"),
    ]
    p._on_rank_detail_done({"tracks": tracks, "is_end": True})
    assert p.list_rank_songs.count() == 2
    assert "1." in p.list_rank_songs.item(0).text()
    assert "A - B" in p.list_rank_songs.item(0).text()
    assert "2." in p.list_rank_songs.item(1).text()
    assert "共 2 首" in p.lbl_rank_status.text()


def test_rank_detail_done_empty_degrades(qapp):
    p = _panel()
    p._on_rank_detail_done({"tracks": [], "is_end": True})
    assert p.list_rank_songs.count() == 0
    assert "无歌曲" in p.lbl_rank_status.text()


def test_rank_detail_failed_degrades(qapp):
    p = _panel()
    p._on_rank_detail_failed("boom")
    assert "失败" in p.lbl_rank_status.text()
    assert p.list_rank_songs.count() == 0


def test_rank_song_play_calls_play_online(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    p = _panel()
    p._music.play_online.return_value = True
    t = OnlineTrack(source_name="酷我音乐", track_id="1", title="A", artist="B")
    item = QListWidgetItem("1. A - B")
    item.setData(Qt.UserRole, t)
    p.list_rank_songs.addItem(item)
    p._on_rank_song_play(item)
    p._music.play_online.assert_called_once_with(t)
    assert "正在播放" in p.lbl_rank_status.text()


def test_rank_song_play_failure_degrades(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QListWidgetItem
    p = _panel()
    p._music.play_online.return_value = False
    t = OnlineTrack(source_name="酷我音乐", track_id="1", title="A", artist="B")
    item = QListWidgetItem("1. A - B")
    item.setData(Qt.UserRole, t)
    p.list_rank_songs.addItem(item)
    p._on_rank_song_play(item)
    p._music.play_online.assert_called_once_with(t)
    assert "无法播放" in p.lbl_rank_status.text()


# ---------- worker（不起线程，同步 run） ----------

def test_rank_lists_worker_emits_done():
    from app.ui.panels.music_panel import _RankListsWorker
    fake = MagicMock()
    fake.get_top_lists.return_value = {"kw": []}
    w = _RankListsWorker(fake)
    got = {}
    w.done.connect(lambda d: got.update(d))
    w.run()
    assert got == {"kw": []}


def test_rank_lists_worker_failed():
    from app.ui.panels.music_panel import _RankListsWorker
    fake = MagicMock()
    fake.get_top_lists.side_effect = RuntimeError("boom")
    w = _RankListsWorker(fake)
    errs = []
    w.failed.connect(errs.append)
    w.run()
    assert errs == ["boom"]


def test_rank_detail_worker_emits_done():
    from app.ui.panels.music_panel import _RankDetailWorker
    fake = MagicMock()
    fake.get_top_list_detail.return_value = {"is_end": True, "tracks": []}
    w = _RankDetailWorker(fake, "kw", "93")
    got = {}
    w.done.connect(lambda d: got.update(d))
    w.run()
    assert got == {"is_end": True, "tracks": []}
    fake.get_top_list_detail.assert_called_once_with("kw", "93")


# ---------- QThread 崩溃修复：防重入 + 最新请求补偿 ----------

def _fake_qthread(monkeypatch, running=False):
    """monkeypatch music_panel.QThread 为真实 QThread 子类替身：
    moveToThread 类型校验通过，但 start() 不真起线程（记录调用）。
    """
    from PySide6.QtCore import QThread

    class _FakeQThread(QThread):
        def __init__(self):
            super().__init__()
            self.running = running
            self.started_calls = 0

        def isRunning(self):
            return self.running

        def start(self, *a, **k):
            self.started_calls += 1
            self.running = True

        def quit(self):
            self.running = False

    monkeypatch.setattr("app.ui.panels.music_panel.QThread", _FakeQThread)
    return _FakeQThread


def test_rank_thread_fields_initialized(qapp):
    p = _panel()
    assert p._rank_thread is None
    assert p._rank_detail_thread is None
    assert p._pending_rank_lists is False
    assert p._pending_rank_detail is None


def test_rank_detail_quick_click_a_then_b_no_crash(qapp, monkeypatch):
    """快速连点 A→B（旧详情线程运行中）：不新建不覆盖，pending 记最新。"""
    _fake_qthread(monkeypatch)
    p = _panel()
    p._load_rank_detail("kw", "1", "热歌榜")      # 正常创建，线程 running=True
    assert p._rank_detail_thread.started_calls == 1
    p._load_rank_detail("tx", "2", "飙升榜")      # 运行中 → 防重入
    assert p._pending_rank_detail == {"source": "tx", "list_id": "2", "title": "飙升榜"}
    assert p._rank_detail_thread.started_calls == 1   # 未新建未覆盖


def test_rank_detail_normal_starts_thread(qapp, monkeypatch):
    _fake_qthread(monkeypatch)
    p = _panel()
    p._load_rank_detail("kw", "1", "热歌榜")
    assert p._rank_detail_thread.started_calls == 1
    assert p._pending_rank_detail is None


def test_rank_detail_done_compensates_pending(qapp):
    """旧线程完成 → 自动加载最新请求（不丢点击）。"""
    p = _panel()
    p._pending_rank_detail = {"source": "tx", "list_id": "2", "title": "飙升榜"}
    calls = []
    p._load_rank_detail = lambda s, lid, t: calls.append((s, lid, t))
    p._on_rank_detail_done({"tracks": [], "is_end": True})
    assert calls == [("tx", "2", "飙升榜")]
    assert p._pending_rank_detail is None


def test_rank_detail_failed_compensates_pending(qapp):
    p = _panel()
    p._pending_rank_detail = {"source": "tx", "list_id": "2", "title": "飙升榜"}
    calls = []
    p._load_rank_detail = lambda s, lid, t: calls.append((s, lid, t))
    p._on_rank_detail_failed("boom")
    assert calls == [("tx", "2", "飙升榜")]
    assert p._pending_rank_detail is None


def test_rank_detail_done_no_pending_keeps_silent(qapp):
    p = _panel()
    calls = []
    p._load_rank_detail = lambda s, lid, t: calls.append((s, lid, t))
    p._on_rank_detail_done({"tracks": [], "is_end": True})
    assert calls == []


def test_rank_lists_reentry_pending_no_new_thread(qapp, monkeypatch):
    """旧分组线程运行中 → 不新建，记 pending。"""
    _fake_qthread(monkeypatch)
    p = _panel()
    p._load_top_lists()                          # 正常创建，running=True
    assert p._rank_thread.started_calls == 1
    p._load_top_lists()                          # 运行中 → 防重入
    assert p._pending_rank_lists is True
    assert p._rank_thread.started_calls == 1


def test_rank_lists_normal_starts_thread(qapp, monkeypatch):
    _fake_qthread(monkeypatch)
    p = _panel()
    p._load_top_lists()
    assert p._rank_thread.started_calls == 1
    assert p._pending_rank_lists is False


def test_rank_lists_done_compensates_pending(qapp):
    p = _panel()
    p._pending_rank_lists = True
    calls = []
    p._load_top_lists = lambda: calls.append(1)
    p._on_rank_lists_done({})
    assert calls == [1]
    assert p._pending_rank_lists is False


def test_rank_lists_failed_compensates_pending(qapp):
    p = _panel()
    p._pending_rank_lists = True
    calls = []
    p._load_top_lists = lambda: calls.append(1)
    p._on_rank_lists_failed("boom")
    assert calls == [1]
    assert p._pending_rank_lists is False


def test_do_search_reentry_ignores(qapp, monkeypatch):
    """搜索同款防重入：旧搜索线程运行中 → 忽略（不新建不覆盖）。"""
    _fake_qthread(monkeypatch)
    p = _panel()
    p.edit_kw.setText("晴天")
    p._do_search()                               # 正常创建，running=True
    assert p._thread.started_calls == 1
    p._do_search()                               # 运行中 → 忽略
    assert p._thread.started_calls == 1


def test_do_search_normal_starts(qapp, monkeypatch):
    _fake_qthread(monkeypatch)
    p = _panel()
    p.edit_kw.setText("晴天")
    p._do_search()
    assert p._thread.started_calls == 1
