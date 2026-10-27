"""
插件刷新同步定向测试（任务 A/B/C 核心逻辑）：
- 插件类型全文检测（混淆插件不再误判为 JSON 空壳）；
- OnlineSourceManager.invalidate 清缓存（刷新后搜索不被旧缓存掩盖）；
- MusicController.reload_online_plugins 统一重载入口（真实结果 + 清缓存）；
- JSMusicSource._read_loop 旧进程 EOF 竞态守卫（刷新后搜索失效的根因）。
"""
import os
import sys
import threading
import types
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ---------- 1. 插件类型全文检测 ----------

def _obfuscated_like_kaixin():
    """模拟混淆插件：require(/exports 标记都在第 500 字节之后（开心汽水 实况）。"""
    head = "var _0xod8='jsjiami.com.v7';'use strict';const _0x448874=_0x42e4;(function(_0x32c1b8){return 0;}());"
    pad = "_" * 500
    tail = "Object.defineProperty(exports, '__esModule', {value: true}); const http = require('axios'); module.exports = {platform: 'p', version: '1.0', search: async function(){}};"
    return head + pad + tail


def test_detect_plugin_type_obfuscated_markers_beyond_500():
    """混淆插件的标记在第 500 字节之后 → 全文检测必须识别为 musicfree
    （旧实现只读前 500 字节 → 误判为非插件 → 空壳导入，开心汽水 丢失根因）。"""
    from app.ui.dialogs.source_manage_dialog import detect_plugin_type
    assert detect_plugin_type(_obfuscated_like_kaixin()) == "musicfree"


def test_detect_plugin_type_real_files():
    """真实插件文件分类：musicfree 6 个 + lx 2 个（基于当前 data/music_sources）。"""
    from app.core.path_resolve import PROJECT_ROOT
    from app.ui.dialogs.source_manage_dialog import detect_plugin_type

    base = PROJECT_ROOT / "data" / "music_sources"
    mf_dir = base / "musicfree"
    lx_dir = base / "lx"
    if not mf_dir.is_dir() or not lx_dir.is_dir():
        pytest.skip("插件目录不存在，跳过真实文件断言")

    mf_ok = lx_ok = 0
    for f in sorted(mf_dir.glob("*.js")):
        assert detect_plugin_type(f.read_text(encoding="utf-8")) == "musicfree", f.name
        mf_ok += 1
    for f in sorted(lx_dir.glob("*.js")):
        assert detect_plugin_type(f.read_text(encoding="utf-8")) == "lx", f.name
        lx_ok += 1
    assert mf_ok >= 1 and lx_ok >= 1, "真实插件文件应可被识别"


def test_detect_plugin_type_edge_cases():
    """边界：两类标记并存 → musicfree；无标记 → 按 musicfree 候选（不建空壳）。"""
    from app.ui.dialogs.source_manage_dialog import detect_plugin_type
    assert detect_plugin_type("@name 洛雪\nconst x = require('a');") == "musicfree"
    assert detect_plugin_type("module.exports = {platform:'p'}") == "musicfree"
    assert detect_plugin_type("/*! * @name 洛雪音源 */\nconst DEV=true") == "lx"
    assert detect_plugin_type("console.log('hello')") == "musicfree"
    assert detect_plugin_type("") == "musicfree"


# ---------- 2. OnlineSourceManager.invalidate ----------

def test_invalidate_clears_cache():
    """刷新后清缓存：同关键词再次搜索必须重新调用源（旧缓存不再掩盖新插件）。"""
    from app.music.online.manager import OnlineSourceManager
    from app.music.online.models import OnlineTrack
    from app.music.online.source import MusicSource

    class _Src(MusicSource):
        name = "x"

        def __init__(self):
            self.calls = 0

        def search(self, keyword, limit=20):
            self.calls += 1
            return [OnlineTrack(source_name="x", track_id=str(self.calls), title=keyword)]

        def get_play_url(self, track):
            return "u"

        def get_lyric(self, track):
            return None

    m = OnlineSourceManager()
    s = _Src()
    m.register(s)
    try:
        m.search_all("k", limit_per_source=5)
        assert s.calls == 1
        m.search_all("k", limit_per_source=5)   # 命中缓存
        assert s.calls == 1
        m.invalidate("x")                        # 刷新后失效缓存
        m.search_all("k", limit_per_source=5)   # 重新拉取
        assert s.calls == 2
    finally:
        m.close()


# ---------- 3. MusicController.reload_online_plugins ----------

class _FakeJS:
    def __init__(self, ok=True, n=3, reload_error=None):
        self._ok = ok
        self._n = n
        self._err = reload_error

    def reload(self):
        if self._err:
            raise self._err
        return self._ok

    def list_plugins(self):
        return [{"name": f"p{i}", "status": "ok"} for i in range(self._n)]


class _FakeOM:
    def __init__(self, js=None):
        self._js = js
        self.invalidated = []

    def get_source(self, name):
        return self._js

    def invalidate(self, name):
        self.invalidated.append(name)


def _ctrl(om):
    from app.music.controller import MusicController
    c = MusicController.__new__(MusicController)
    c.online_manager = om
    return c


def test_reload_online_plugins_ok_invalidates_cache():
    om = _FakeOM(_FakeJS(ok=True, n=9))
    c = _ctrl(om)
    ok, n = c.reload_online_plugins()
    assert ok is True and n == 9
    assert om.invalidated == ["js_bridge"]


def test_reload_online_plugins_failure_preserves_error_state():
    om = _FakeOM(_FakeJS(reload_error=RuntimeError("node down")))
    c = _ctrl(om)
    ok, n = c.reload_online_plugins()
    assert ok is False, "重载异常必须报告失败，不假装成功"
    assert n == 3, "插件数取 list_plugins 实际值"
    assert om.invalidated == ["js_bridge"], "失败也必须清缓存，避免旧结果回读"


def test_reload_online_plugins_no_manager_or_no_js():
    from app.music.controller import MusicController
    c = MusicController.__new__(MusicController)
    c.online_manager = None
    assert c.reload_online_plugins() == (False, 0)
    c.online_manager = _FakeOM(None)
    assert c.reload_online_plugins() == (False, 0)


# ---------- 4. JSMusicSource._read_loop 旧进程 EOF 竞态守卫 ----------

class _FiniteStdout:
    """有限行迭代器：迭代完触发 on_finish（模拟循环读取期间 self._proc 被换掉）。"""

    def __init__(self, lines, on_finish=None):
        self._it = iter(lines)
        self._on_finish = on_finish

    def __iter__(self):
        return self

    def __next__(self):
        try:
            return next(self._it)
        except StopIteration:
            if self._on_finish:
                self._on_finish()
            raise


def _mk_src(proc):
    from app.music.online.sources.js_source import JSMusicSource
    src = JSMusicSource.__new__(JSMusicSource)
    src._proc = proc
    src._sources = []
    src._plugins = []
    src._pending = {}
    src._ready = threading.Event()
    src._lock = threading.Lock()
    src._call_timeout_streak = 0
    src._auto_reloading = False
    src._auto_reload_count = 0
    src._auto_reload_window_ts = 0.0
    src._auto_reload_gave_up = False
    src._closed = False
    return src


def test_read_loop_stale_proc_eof_does_not_auto_reload():
    """手动 reload() 后，被终止的旧进程 EOF 不得触发自动恢复（防双 reload 竞态
    反复杀新进程 → 刷新后搜索失效）。"""
    from app.music.online.sources.js_source import JSMusicSource

    proc_old = types.SimpleNamespace(stdout=None)
    proc_new = types.SimpleNamespace(stdout=None)
    src = _mk_src(proc_old)

    def swap_to_new():
        src._proc = proc_new   # 模拟 reload() 已把 self._proc 指向新进程

    proc_old.stdout = _FiniteStdout(["", "not-json\n"], swap_to_new)
    src._maybe_auto_reload = MagicMock()
    src._read_loop()
    src._maybe_auto_reload.assert_not_called()


def test_read_loop_current_proc_eof_triggers_auto_reload():
    """当前（存活）进程自己退出 → 仍触发自动恢复（watchdog 能力不被削弱）。"""
    from app.music.online.sources.js_source import JSMusicSource

    proc = types.SimpleNamespace(stdout=None)
    src = _mk_src(proc)
    proc.stdout = _FiniteStdout(["", "not-json\n"])
    src._maybe_auto_reload = MagicMock()
    src._read_loop()
    src._maybe_auto_reload.assert_called_once_with("node-exit")


def test_read_loop_ready_message_fills_plugins():
    """_ready 消息正常填充 _sources/_plugins（重载后注册表即最新）；
    消息读完进程退出时按「当前进程」触发自动恢复（watchdog 正常路径）。"""
    from app.music.online.sources.js_source import JSMusicSource

    proc = types.SimpleNamespace(stdout=None)
    src = _mk_src(proc)
    payload = ('{"id":"_ready","ok":true,"sources":["a","b"],'
               '"plugins":[{"name":"a"},{"name":"b"}]}\n')
    proc.stdout = _FiniteStdout([payload])
    src._maybe_auto_reload = MagicMock()
    src._read_loop()
    assert src._sources == ["a", "b"]
    assert len(src._plugins) == 2
    assert src._ready.is_set()
    # 当前进程退出 → 触发自动恢复（与旧进程 EOF 被守卫跳过形成对照）
    src._maybe_auto_reload.assert_called_once_with("node-exit")


def test_read_loop_reload_does_not_break_playback_chain():
    """重载只动 js_source 与缓存，不触碰播放链（mpv/播放器不受影响）。
    以 reload_online_plugins 的调用面为证据：仅用 online_manager，无播放 API。"""
    om = _FakeOM(_FakeJS(ok=True, n=8))
    c = _ctrl(om)
    ok, n = c.reload_online_plugins()
    assert ok and n == 8
    # 调用面最小化：只允许 reload/list_plugins/invalidate/get_source
    assert set(om.__dict__.keys()) == {"_js", "invalidated"}
