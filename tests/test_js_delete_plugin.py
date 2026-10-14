"""JSMusicSource.delete_plugin 单测（不真起 node，用 tmp 文件系统 + mock reload）。

覆盖：
- file 字段定位（bridge 改造后成功/失败插件都带 file）
- lx 插件兜底拼 lx_dir/{name}.js（中文文件名）
- 找不到 / 非 .js / 目录外（含前缀误判：lx vs lx_evil）拒绝
- os.remove 异常 / reload 失败路径
"""
import threading

from app.music.online.sources.js_source import JSMusicSource


def _make_source(mf_dir, lx_dir, plugins):
    s = JSMusicSource.__new__(JSMusicSource)
    s._proc = None
    s._lock = threading.Lock()
    s._pending = {}
    s._sources = []
    s._plugins = plugins
    s._mf_dir = str(mf_dir)
    s._lx_dir = str(lx_dir)
    return s


def test_delete_plugin_ok_via_file(tmp_path, monkeypatch):
    """file 字段优先定位：真实文件被删、reload 被调、返回 ok。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    target = lx_dir / "落雪.js"
    target.write_text("// bad plugin", encoding="utf-8")
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "落雪", "type": "lx", "status": "fail",
         "error": "当前客户端不支持", "file": str(target)},
    ])
    monkeypatch.setattr(s, "reload", lambda: True)
    out = s.delete_plugin("落雪")
    assert out == {"ok": True, "error": None}
    assert not target.exists()


def test_delete_plugin_fallback_lx_name(tmp_path, monkeypatch):
    """无 file 字段（旧 bridge 形态）→ 兜底 lx_dir/{name}.js，中文名可拼。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    target = lx_dir / "野花.js"
    target.write_text("// bad", encoding="utf-8")
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "野花", "type": "lx", "status": "fail", "error": "Cannot read..."},
    ])
    monkeypatch.setattr(s, "reload", lambda: True)
    out = s.delete_plugin("野花")
    assert out == {"ok": True, "error": None}
    assert not target.exists()


def test_delete_plugin_unknown_name(tmp_path):
    """name 不在 _plugins 且兜底文件不存在 → 不删任何文件。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    s = _make_source(tmp_path / "mf", lx_dir, [])
    out = s.delete_plugin("不存在.js")
    assert out["ok"] is False
    assert "找不到插件文件" in out["error"]
    assert not list(lx_dir.iterdir())


def test_delete_plugin_prefix_traversal_rejected(tmp_path, monkeypatch):
    """前缀误判修复：lx_evil 是 lx 的字符串前缀但非子目录 → 必须拒绝（relative_to）。"""
    lx_dir = tmp_path / "lx"
    evil_dir = tmp_path / "lx_evil"
    evil_dir.mkdir(parents=True)
    target = evil_dir / "bad.js"
    target.write_text("// evil", encoding="utf-8")
    # 旧实现 str.startswith(str(lx_dir)) 会误判 True；relative_to 应拒绝
    assert str(target).startswith(str(lx_dir))
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "bad", "type": "lx", "status": "fail",
         "error": "x", "file": str(target)},
    ])
    monkeypatch.setattr(s, "reload", lambda: True)
    out = s.delete_plugin("bad")
    assert out["ok"] is False
    assert "拒绝删除音源目录外文件" in out["error"]
    assert target.exists()   # 文件未被删


def test_delete_plugin_non_js_rejected(tmp_path, monkeypatch):
    """非 .js 后缀 → 拒绝，不删。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    target = lx_dir / "bad.txt"
    target.write_text("x", encoding="utf-8")
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "bad", "type": "lx", "status": "fail",
         "error": "x", "file": str(target)},
    ])
    monkeypatch.setattr(s, "reload", lambda: True)
    out = s.delete_plugin("bad")
    assert out["ok"] is False
    assert "拒绝删除非插件文件" in out["error"]
    assert target.exists()


def test_delete_plugin_reload_fail(tmp_path, monkeypatch):
    """文件已删但 reload 失败 → {ok:False, error:'reload-fail'}。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    target = lx_dir / "lx.js"
    target.write_text("// bad", encoding="utf-8")
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "lx", "type": "lx", "status": "fail",
         "error": "rawScript", "file": str(target)},
    ])
    monkeypatch.setattr(s, "reload", lambda: False)
    out = s.delete_plugin("lx")
    assert out == {"ok": False, "error": "reload-fail"}
    assert not target.exists()


def test_delete_plugin_os_error(tmp_path, monkeypatch):
    """os.remove 异常 → 异常不冒泡，返回 {ok:False, error}。"""
    lx_dir = tmp_path / "lx"
    lx_dir.mkdir()
    target = lx_dir / "lx (2).js"
    target.write_text("// bad", encoding="utf-8")
    s = _make_source(tmp_path / "mf", lx_dir, [
        {"name": "lx (2)", "type": "lx", "status": "fail",
         "error": "name", "file": str(target)},
    ])
    import os
    monkeypatch.setattr(os, "remove", lambda p: (_ for _ in ()).throw(PermissionError("locked")))
    out = s.delete_plugin("lx (2)")
    assert out["ok"] is False
    assert "locked" in out["error"]
    assert target.exists()
