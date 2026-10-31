"""搜索结果、失败缓存及失效代次回归，全部使用内存音源。"""
import threading
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from app.music.online.sources.js_source import JSMusicSource
from app.music.online.source import SourceSearchError


class Source:
    def __init__(self, name, answer):
        self.name, self.answer, self.calls = name, answer, 0

    def search(self, keyword, limit):
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer

    def close(self): pass


def js_source(monkeypatch, replies):
    s = JSMusicSource.__new__(JSMusicSource)
    calls = []
    def call(*args, **kwargs):
        calls.append(args)
        return replies[min(len(calls) - 1, len(replies) - 1)]
    monkeypatch.setattr(s, "_call", call)
    monkeypatch.setattr(s, "close", lambda: None)
    return s, calls


def test_js_unknown_failure_is_not_cached_and_same_keyword_retries(monkeypatch):
    s, calls = js_source(monkeypatch, [None, []])
    m = OnlineSourceManager()
    m.register(s)
    try:
        failed = m.search_all("A")
        assert getattr(failed, "errors", {}).get("js_bridge") == "unknown"
        assert m._cache.get("js_bridge", "A", 10) is None
        recovered = m.search_all("A")
        assert len(calls) == 2
        assert not recovered.errors
        assert recovered == {"js_bridge": []}
    finally:
        m.close()


def test_successful_empty_search_remains_cacheable(monkeypatch):
    s, calls = js_source(monkeypatch, [[]])
    m = OnlineSourceManager()
    m.register(s)
    try:
        first = m.search_all("A")
        second = m.search_all("A")
        assert first == second == {"js_bridge": []}
        assert len(calls) == 1
        assert getattr(second, "successful_sources", set()) == {"js_bridge"}
        assert not second.errors
    finally:
        m.close()


@pytest.mark.parametrize("all_fail", [False, True])
def test_aggregate_preserves_per_request_success_and_errors(all_fail):
    m = OnlineSourceManager()
    m.register(Source("broken", ValueError("fixture plugin failure")))
    good = Source("good", RuntimeError("fixture failure") if all_fail else [OnlineTrack("good", "1", "A")])
    m.register(good)
    try:
        result = m.search_all("A")
        assert getattr(result, "errors", None) is not None
        assert "broken" in result.errors
        assert result.successful_sources == (set() if all_fail else {"good"})
        if not all_fail:
            assert result["good"][0].title == "A"
        good.answer = []
        m.invalidate("good")
        retry = m.search_all("A")
        assert "good" not in retry.errors
        assert "good" in retry.successful_sources
    finally:
        m.close()


def test_legacy_none_does_not_claim_no_results_or_timeout():
    m = OnlineSourceManager()
    s = Source("legacy", None)
    m.register(s)
    try:
        result = m.search_all("A")
        assert getattr(result, "errors", {}).get("legacy") == "unknown"
        m.search_all("A")
        assert s.calls == 2
    finally:
        m.close()


def test_invalidate_rejects_inflight_result_and_preserves_normal_cache():
    entered, release = threading.Event(), threading.Event()
    s = Source("js_bridge", None)
    def search(keyword, limit):
        s.calls += 1
        if s.calls == 1:
            entered.set()
            assert release.wait(3)
            return [OnlineTrack(s.name, "old", "old")]
        return [OnlineTrack(s.name, "new", "new")]
    s.search = search
    m = OnlineSourceManager()
    m.register(s)
    old = []
    thread = threading.Thread(target=lambda: old.append(m.search_all("A")))
    try:
        thread.start()
        assert entered.wait(2)
        m.invalidate(s.name)
        release.set()
        thread.join(3)
        assert not thread.is_alive()
        assert old == [{}]
        assert old[0].errors == {s.name: "interrupted"}
        fresh = m.search_all("A")
        cached = m.search_all("A")
        assert fresh[s.name][0].title == cached[s.name][0].title == "new"
        assert s.calls == 2
    finally:
        release.set()
        thread.join(3)
        m.close()


def test_js_partial_failure_keeps_tracks_but_is_not_cached(monkeypatch):
    s, calls = js_source(monkeypatch, [{
        "tracks": [{"source_name": "ok", "track_id": "1", "title": "A"}],
        "errors": {"broken": "plugin"}, "successful": 1,
    }])
    m = OnlineSourceManager()
    m.register(s)
    try:
        result = m.search_all("A")
        assert result["js_bridge"][0].title == "A"
        assert result.errors["js_bridge"] == "plugin"
        assert result.successful_sources == {"js_bridge"}
        m.search_all("A")
        assert len(calls) == 2
    finally:
        m.close()


@pytest.mark.parametrize("kind", ["none", "timeout", "unavailable", "plugin"])
def test_search_failure_does_not_prevent_immediate_retry(monkeypatch, kind):
    s, calls = js_source(monkeypatch, [[]])
    def call(*args, **kwargs):
        calls.append(args)
        if len(calls) == 1:
            if kind == "none":
                return None
            raise SourceSearchError(kind)
        return []
    monkeypatch.setattr(s, "_call", call)
    m = OnlineSourceManager()
    m.register(s)
    try:
        first = m.search_all("A")
        assert first.errors == {"js_bridge": "unknown" if kind == "none" else kind}
        assert not first.successful_sources
        second = m.search_all("A")
        assert not second.errors and second.successful_sources == {"js_bridge"}
        assert len(calls) == 2
    finally:
        m.close()


@pytest.mark.parametrize("failure,category", [
    ("missing", "unavailable"), ("write", "unavailable"),
    ("timeout", "timeout"), ("rejected", "plugin"), ("interrupted", "unknown"),
])
def test_real_rpc_search_maps_failure_without_changing_other_methods(failure, category):
    s = JSMusicSource.__new__(JSMusicSource)
    s._lock = threading.Lock()
    s._pending = {}
    s._note_call_timeout = lambda: None
    def write(line):
        if failure == "write":
            raise BrokenPipeError("fixture")
        if failure in {"rejected", "interrupted"}:
            box = s._pending.pop(json.loads(line)["id"])
            box["result"] = {"ok": False, "error": "fixture"} if failure == "rejected" else None
            box["event"].set()
    s._proc = None if failure == "missing" else SimpleNamespace(
        stdin=SimpleNamespace(write=write, flush=lambda: None))
    with pytest.raises(SourceSearchError) as exc:
        s._call("search", {}, timeout=.001)
    assert exc.value.category == category
    assert s._pending == {}
    # 播放 URL、歌词等旧 RPC 仍遵循原来的 None 失败契约。
    assert s._call("getUrl", {}, timeout=.001) is None
    assert s._pending == {}


@pytest.mark.parametrize("case", ["empty", "failed", "partial", "unavailable"])
def test_real_node_bridge_preserves_search_failures_without_user_plugins(case):
    """执行真实 bridge.js；沙箱只提供内存插件，不读取用户插件或请求网络。"""
    node = shutil.which("node")
    if not node:
        bundled = Path(__file__).resolve().parents[2] / "third_party/node/node.exe"
        assert bundled.is_file(), "本项桥接集成测试需要现有 Node.js"
        node = str(bundled)
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    script = r'''
const fs = require('fs'), vm = require('vm');
const code = fs.readFileSync(process.argv[1], 'utf8');
const scenario = process.argv[2];
let handler;
const loader = {
  load(fp) { return { name: fp.includes('bad') ? 'bad' : 'good', type:'musicfree' }; },
  async callSearch(p) { if (p.name === 'bad') throw Error('fixture parse error'); return []; }
};
const fakeFs = {existsSync:()=>true, readdirSync:()=>scenario==='unavailable'?[]:
  scenario==='empty'?['good.js']:scenario==='failed'?['bad.js']:['bad.js','good.js']};
const ctx = {require:n=>n==='fs'?fakeFs:n==='path'?require('path'):
  n.includes('jsonrpc')?{startRpc:h=>{handler=h;}}:loader,
  process:{env:{MF_PLUGIN_DIR:'/virtual'},on(){},stdout:{write(){}}}, console:{error(){}}};
vm.runInNewContext(code, ctx);
setImmediate(async()=>{
  try { process.stdout.write(JSON.stringify(await handler('search',{source:'*',keyword:'A',limit:3}))); }
  catch(e) { console.error(e);process.exitCode=1; }
});
'''
    proc = subprocess.run([node, "-e", script, str(bridge), case],
                          capture_output=True, text=True, encoding="utf-8", timeout=10,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout)
    assert result["tracks"] == []
    assert bool(result["successful"]) == (case in {"empty", "partial"})
    assert bool(result["errors"]) == (case != "empty")
    if case in {"failed", "partial"}:
        assert result["errors"] == {"bad": "plugin"}
    elif case == "unavailable":
        assert result["errors"] == {"js_bridge": "unavailable"}
