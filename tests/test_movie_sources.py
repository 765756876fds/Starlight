# -*- coding: utf-8 -*-
"""电影源管理测试：多源增删/持久化/合并、三合一测源、搜索策略 C、UI 入口。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import MagicMock, patch

import pytest

from PySide6.QtWidgets import QApplication, QMessageBox

_app = QApplication.instance() or QApplication([])

from app.movie.source_manager import (
    DEFAULT_SOURCES,
    MovieSourceManager,
    _check_hls,
    _SingleSourceManager,
)
from app.movie.movie_controller import MovieController
from app.ui.dialogs.movie_source_dialog import MovieSourceDialog
from app.ui.panels.movie_panel import MoviePanel


# ---------- manager：默认源 / 增删 / 持久化 / 用户删除永久 ----------

def _mgr(tmp_path):
    from pathlib import Path
    return MovieSourceManager(config_path=str(Path(tmp_path) / "settings.yaml"))


def test_default_sources_loaded():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        m = MovieSourceManager(config_path=os.path.join(d, "s.yaml"))
        srcs = m.get_all_sources()
        assert len(srcs) == 9
        assert srcs[0]["name"] == "量子资源"  # 量子置顶
        assert m.get_active_name() == "量子资源"


def test_add_enable_remove_set_active(tmp_path):
    m = _mgr(tmp_path)
    assert m.add_source("我的源", "https://x.com/api.php/provide/vod/") is True
    assert m.add_source("我的源", "https://y.com/") is False  # 重名
    assert m.add_source("", "https://z.com/") is False
    assert m.add_source("z", "") is False
    srcs = m.get_all_sources()
    assert any(s["name"] == "我的源" for s in srcs)

    m.enable_source("量子资源", False)
    assert not [s for s in m.get_enabled_sources() if s["name"] == "量子资源"]

    m.set_active("我的源")
    assert m.get_active_name() == "我的源"

    m.remove_source("我的源")
    assert not any(s["name"] == "我的源" for s in m.get_all_sources())
    # active 被删 → 回退第一个
    assert m.get_active_name() == "量子资源"


def test_persistence(tmp_path):
    p = str(tmp_path / "settings.yaml")
    m1 = MovieSourceManager(config_path=p)
    m1.add_source("自建源A", "https://a.com/api.php/provide/vod/")
    m1.set_active("自建源A")
    m1.enable_source("非凡资源", False)

    m2 = MovieSourceManager(config_path=p)  # 重新加载（持久化生效）
    names = [s["name"] for s in m2.get_all_sources()]
    assert "自建源A" in names
    assert m2.get_active_name() == "自建源A"
    assert not [s for s in m2.get_enabled_sources() if s["name"] == "非凡资源"]


def test_user_removed_default_not_revived(tmp_path):
    """用户删除永久：写回一次后，默认源不复活。"""
    p = str(tmp_path / "settings.yaml")
    m1 = MovieSourceManager(config_path=p)
    assert len(m1.get_all_sources()) == 9
    m1.remove_source("量子资源")  # 触发写回（settings.movie.sources 接管）

    m2 = MovieSourceManager(config_path=p)
    names = [s["name"] for s in m2.get_all_sources()]
    assert "量子资源" not in names
    assert len(names) == 8


# ---------- _save 防覆盖写（P1） ----------

def test_save_aborts_when_yaml_corrupt(tmp_path):
    """文件存在但 yaml 损坏（_read_yaml 返回 {}）→ 写操作中止，不覆盖原文。"""
    p = str(tmp_path / "settings.yaml")
    corrupt = "app: {oops: [unterminated\n  music:\n    volume: 50\n"
    with open(p, "w", encoding="utf-8") as f:
        f.write(corrupt)
    m = MovieSourceManager(config_path=p)
    # 构造时读失败（_sources 回退默认、_active 为空）——任何写操作都必须中止
    m.add_source("新源", "https://new/api.php/provide/vod/")
    m.set_active("量子资源")
    m.remove_source("量子资源")
    with open(p, encoding="utf-8") as f:
        assert f.read() == corrupt  # 原损坏内容原样保留（没被写回）


def test_save_aborts_when_yaml_empty(tmp_path):
    """文件存在但 0 字节（读出来 {}）→ 保守中止，不写回。"""
    p = str(tmp_path / "settings.yaml")
    open(p, "wb").close()
    m = MovieSourceManager(config_path=p)
    m.set_active("量子资源")
    assert open(p, "rb").read() == b""  # 空文件未被写


def test_save_writes_when_file_absent(tmp_path):
    """文件不存在（新配置）→ 正常写回（防覆盖写不拦截首建）。"""
    p = str(tmp_path / "settings.yaml")
    m = MovieSourceManager(config_path=p)
    m.set_active("量子资源")
    import yaml as _yaml
    data = _yaml.safe_load(open(p, encoding="utf-8"))
    assert data["movie"]["active_source"] == "量子资源"


# ---------- 三合一测源 ----------

def _resp(status=200, text="{}"):
    r = MagicMock()
    r.status_code = status
    r.text = text
    return r


def _hls_resp():
    r = MagicMock()
    r.iter_content.return_value = iter([b"#EXTM3U\n#EXT-X-STREAM-INF:1\nhttp://x/a.m3u8\n"])
    return r


def test_test_source_ok():
    body = '{"list": [{"vod_name": "流浪地球2", "vod_play_url": "正片$https://x/index.m3u8"}]}'
    with patch("app.movie.source_manager.requests.get") as m_get:
        m_get.side_effect = [_resp(200, body), _hls_resp()]
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/api.php/provide/vod/"})
        assert ok is True
        assert detail == "可用"
        assert m_get.call_count == 2


def test_test_source_http_fail():
    with patch("app.movie.source_manager.requests.get") as m_get:
        m_get.return_value = _resp(403, "<html>")
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/"})
        assert ok is False
        assert "403" in detail


def test_test_source_timeout():
    with patch("app.movie.source_manager.requests.get", side_effect=__import__("requests").exceptions.Timeout()):
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/"})
        assert ok is False
        assert detail == "超时"


def test_test_source_non_json():
    with patch("app.movie.source_manager.requests.get") as m_get:
        m_get.return_value = _resp(200, "<html>page")
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/"})
        assert ok is False
        assert "非 JSON" in detail


def test_test_source_no_result():
    with patch("app.movie.source_manager.requests.get") as m_get:
        m_get.return_value = _resp(200, '{"list": []}')
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/"})
        assert ok is False
        assert "无结果" in detail


def test_test_source_no_m3u8_url():
    body = '{"list": [{"vod_name": "x", "vod_play_url": "无分隔URL"}]}'
    with patch("app.movie.source_manager.requests.get") as m_get:
        m_get.return_value = _resp(200, body)
        ok, detail = MovieSourceManager.test_source({"name": "a", "url": "https://s/"})
        assert ok is False
        assert "m3u8" in detail


def test_test_source_empty_url():
    ok, detail = MovieSourceManager.test_source({})
    assert ok is False


def test_check_hls_non_hls():
    r = MagicMock()
    r.iter_content.return_value = iter([b"<html>not hls</html>"])
    with patch("app.movie.source_manager.requests.get", return_value=r):
        assert _check_hls("https://x/a.m3u8", timeout=5) is False


def test_test_all():
    m = _mgr(__import__("tempfile").mkdtemp())
    with patch.object(m, "test_source") as m_t:
        m_t.side_effect = [(True, "可用"), (False, "超时")]
        out = m.test_all([{"name": "a", "url": "u1"}, {"name": "b", "url": "u2"}])
        assert out == [("a", True, "可用"), ("b", False, "超时")]


# ---------- 搜索策略 C ----------

class _FakeMgr:
    def __init__(self, sources, active):
        self._srcs = sources
        self._active = active

    def get_enabled_sources(self):
        return [dict(s) for s in self._srcs if s.get("enabled", True)]

    def get_active_name(self):
        return self._active


def _ctrl(manager):
    c = MovieController.__new__(MovieController)
    c._source_manager = manager
    c.last_search_source = None
    c.last_search_error = ""
    return c


def _src(name):
    return {"name": name, "url": f"https://{name}/api.php/provide/vod/", "enabled": True}


def _items(name):
    return [{"vod_id": 1, "vod_name": "流浪地球2", "play_url": "正片$https://x/m3u8",
             "_source": name}]


def test_search_active_hit():
    c = _ctrl(_FakeMgr([_src("量子资源"), _src("非凡资源")], "量子资源"))
    with patch.object(c, "_search_one", return_value=_items("量子资源")) as m1:
        out = c.search("流浪地球2")
        assert out and out[0]["_source"] == "量子资源"
        assert c.last_search_source == "量子资源"
        assert m1.call_count == 1  # 当前源命中，不试第二个


def test_search_fallback_when_active_fails():
    """当前源挂了 → 自动换下一个；不改 active_source。"""
    mgr = _FakeMgr([_src("量子资源"), _src("非凡资源"), _src("暴风资源")], "量子资源")
    c = _ctrl(mgr)
    with patch.object(c, "_search_one", side_effect=[RuntimeError("timeout"), _items("非凡资源")]) as m1:
        out = c.search("流浪地球2")
        assert out and out[0]["_source"] == "非凡资源"
        assert c.last_search_source == "非凡资源"
        assert m1.call_count == 2
        # 不自动改 active
        assert mgr._active == "量子资源"


def test_search_skip_empty_result():
    """当前源无结果（非异常）→ 继续下一个。"""
    mgr = _FakeMgr([_src("量子资源"), _src("非凡资源")], "量子资源")
    c = _ctrl(mgr)
    with patch.object(c, "_search_one", side_effect=[[], _items("非凡资源")]):
        out = c.search("x")
        assert out and out[0]["_source"] == "非凡资源"


def test_search_all_fail_max3():
    """全部失败 → [] + last_search_error；最多试 3 个（不试第 4 个）。"""
    mgr = _FakeMgr([_src("a"), _src("b"), _src("c"), _src("d")], "a")
    c = _ctrl(mgr)
    with patch.object(c, "_search_one", side_effect=RuntimeError("x")) as m1:
        out = c.search("流浪地球2")
        assert out == []
        assert c.last_search_source is None
        assert "a" in c.last_search_error
        assert m1.call_count == 3  # ≤3


def test_search_no_enabled_sources():
    mgr = _FakeMgr([{**_src("a"), "enabled": False}], "a")
    c = _ctrl(mgr)
    assert c.search("x") == []
    assert c.last_search_error == "无可用源"


def test_search_empty_kw():
    c = _ctrl(_FakeMgr([_src("a")], "a"))
    assert c.search("") == []
    assert c.search("  ") == []


# ---------- 单源兼容（旧行为） ----------

def test_single_source_manager_compat():
    c = MovieController.__new__(MovieController)
    c._source_manager = _SingleSourceManager("https://old/api.php/provide/vod/")
    c.last_search_source = None
    c.last_search_error = ""
    with patch.object(c, "_search_one", return_value=_items("单源")):
        out = c.search("流浪地球2")
        assert out and out[0]["_source"] == "单源"
        assert c.last_search_source == "单源"


# ---------- panel：源按钮 + 当前源显示 ----------

class _FakeMovie:
    def __init__(self, mgr):
        self._source_manager = mgr
        self.play_calls = []

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
        return ""


def _panel(mgr=None):
    mgr = mgr or _FakeMgr([_src("量子资源"), _src("非凡资源")], "量子资源")
    return MoviePanel(movie=_FakeMovie(mgr))


def test_panel_source_button_and_label():
    p = _panel()
    assert p.btn_source.text() == "源"
    assert p.lbl_source.text() == "当前源：量子资源"


def test_panel_refresh_source_label():
    p = _panel()
    p._movie._source_manager._active = "非凡资源"
    p._refresh_source_label()
    assert p.lbl_source.text() == "当前源：非凡资源"


def test_panel_open_source_dialog():
    p = _panel()
    with patch("app.ui.panels.movie_panel.MovieSourceDialog") as m_dlg:
        p._on_source_manage()
        assert m_dlg.call_count == 1
        assert m_dlg.call_args.kwargs["manager"] is p._movie._source_manager  # 传入同一 manager


# ---------- dialog ----------

def test_dialog_rows_and_add(tmp_path):
    m = MovieSourceManager(config_path=str(tmp_path / "s.yaml"))
    dlg = MovieSourceDialog(manager=m)
    assert dlg.list_widget.count() == 9
    dlg.inp_name.setText("自建源B")
    dlg.inp_url.setText("https://b.com/api.php/provide/vod/")
    with patch.object(QMessageBox, "information"):
        dlg._add_source()
    assert any(s["name"] == "自建源B" for s in m.get_all_sources())
    assert dlg.list_widget.count() == 10


def test_dialog_set_active_and_delete(tmp_path):
    m = MovieSourceManager(config_path=str(tmp_path / "s.yaml"))
    dlg = MovieSourceDialog(manager=m)
    # 设为当前
    row0 = dlg.list_widget.item(0)
    dlg.list_widget.setCurrentItem(row0)
    dlg._set_active()
    assert m.get_active_name() == "量子资源"
    # 删除（_set_active 重建列表后需重新选中；mock 确认弹窗 Yes）
    dlg.list_widget.setCurrentItem(dlg.list_widget.item(0))
    with patch.object(QMessageBox, "question", return_value=QMessageBox.Yes):
        dlg._delete_selected()
    assert not any(s["name"] == "量子资源" for s in m.get_all_sources())


def test_dialog_checkbox_enable(tmp_path):
    m = MovieSourceManager(config_path=str(tmp_path / "s.yaml"))
    dlg = MovieSourceDialog(manager=m)
    item = dlg.list_widget.item(1)  # 非凡资源
    item.setCheckState(__import__("PySide6.QtCore", fromlist=["Qt"]).Qt.Unchecked)
    assert not [s for s in m.get_enabled_sources() if s["name"] == "非凡资源"]
