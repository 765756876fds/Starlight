"""Bounded real Node fixtures; no network, real plugins or user data."""
import concurrent.futures
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from app.music.online.sources.js_source import JSMusicSource
from app.music.online.source import SourceSearchError


NODE = Path(__file__).resolve().parents[2] / "third_party/node/node.exe"
if not NODE.is_file():
    NODE = Path(shutil.which("node") or str(NODE))
SERVER = r'''
const rl=require('readline').createInterface({input:process.stdin});
console.log(JSON.stringify({id:'_ready',sources:['fixture'],plugins:[]}));
rl.on('line',line=>{let r;try{r=JSON.parse(line)}catch{return}
 const k=r.params.keyword;
 if(k==='late')require('fs').writeFileSync(require('path').join(__dirname,'entered-late'),'entered');
 if(k==='exit'){process.exit(7);return}
 if(k==='hang')return;
 if(k==='invalid'){process.stdout.write('not-json\nnull\n[]\n42\n');}
 if(k==='stdout')for(let i=0;i<3000;i++)console.log('noise-'+i);
 if(k==='stderr')for(let i=0;i<3000;i++)console.error('noise-'+i);
 const result={keyword:k},data=JSON.stringify({id:r.id,ok:true,result})+'\n';
 if(k==='split'){process.stdout.write(data.slice(0,7));setTimeout(()=>process.stdout.write(data.slice(7)),20);return}
 setTimeout(()=>process.stdout.write(data),k==='late'?180:0);
});
'''


def wait(predicate, seconds=2):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if predicate():
            return
        time.sleep(.005)
    assert predicate(), "bounded bridge fixture wait expired"


@pytest.fixture
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("NODE_EXE", str(NODE))
    (tmp_path / "bridge.js").write_text(SERVER, "utf8")
    src = JSMusicSource(str(tmp_path), str(tmp_path), bridge_dir=str(tmp_path))
    assert src.available
    yield src
    proc = src._proc
    src.close()
    assert not src._pending
    if proc:
        assert proc.poll() is not None


@pytest.mark.parametrize("keyword", ["normal", "stdout", "stderr", "split", "invalid"])
def test_protocol_survives_generated_output_and_segmented_json(source, keyword):
    assert source._call("search", {"keyword": keyword}, timeout=2) == {"keyword": keyword}


def test_consecutive_and_concurrent_requests_keep_their_ids(source):
    for i in range(3):
        assert source._call("search", {"keyword": str(i)}, timeout=1)["keyword"] == str(i)
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: source._call("search", {"keyword": str(i)}, timeout=2), range(24)))
    assert [r["keyword"] for r in results] == list(map(str, range(24)))


def test_timeout_late_reply_and_immediate_retry_are_isolated(source):
    with pytest.raises(SourceSearchError):
        source._call("search", {"keyword": "late"}, timeout=.03)
    assert source._call("search", {"keyword": "new"}, timeout=1) == {"keyword": "new"}
    time.sleep(.2)
    assert not source._pending
    assert source._call("search", {"keyword": "after"}, timeout=1)["keyword"] == "after"


def test_invalid_request_frames_do_not_prevent_later_request():
    rpc = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/lib/jsonrpc.js"
    result = subprocess.run([str(NODE), "-e", "require(process.argv[1]).startRpc(async()=>({valid:true}))", str(rpc)],
                            input='null\n[]\n{bad\n{"id":"valid","method":"ping"}\n',
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    assert json.loads(result.stdout) == {"id": "valid", "ok": True, "result": {"valid": True}}


def test_close_rejects_new_calls_and_cannot_resurrect_via_reload(source):
    source.close()
    assert source.reload() is False
    with pytest.raises(SourceSearchError):
        source._call("search", {"keyword": "closed"}, timeout=.1)


def test_automatic_restarts_actually_consume_the_existing_budget(source, monkeypatch):
    from unittest.mock import Mock
    restart = Mock(return_value=True)
    monkeypatch.setattr(source, "reload", restart)
    for _ in range(6):
        source._maybe_auto_reload("controlled failure")
    assert restart.call_count == 3
    assert source._auto_reload_count == 3 and source._auto_reload_gave_up


def test_retired_reader_cannot_publish_ready_or_restart_current_process(source, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import Mock
    old = SimpleNamespace(stdout=['{"id":"_ready","sources":["obsolete"]}\n'])
    restart = Mock()
    monkeypatch.setattr(source, "_maybe_auto_reload", restart)
    source._read_loop(old)
    assert source.list_sources() == ["fixture"]
    restart.assert_not_called()


@pytest.mark.parametrize("action", ["close", "reload"])
def test_lifecycle_change_wakes_pending_call_and_reaps_old_process(source, action):
    old = source._proc
    errors = []
    def work():
        try:
            source._call("search", {"keyword": "late"}, timeout=2)
        except SourceSearchError as e:
            errors.append(e)
    t = threading.Thread(target=work)
    t.start()
    wait(lambda: bool(source._pending))
    wait(lambda: (source._bridge_dir / "entered-late").exists())
    getattr(source, action)()
    t.join(.5)
    assert not t.is_alive() and len(errors) == 1
    assert old.poll() is not None and not source._pending
    if action == "reload":
        assert source._call("search", {"keyword": "fresh"}, timeout=1)["keyword"] == "fresh"


def test_node_exit_wakes_call_and_can_restart(source):
    start = time.monotonic()
    with pytest.raises(SourceSearchError):
        source._call("search", {"keyword": "exit"}, timeout=2)
    assert time.monotonic() - start < 1
    wait(lambda: source.available)
    assert source._call("search", {"keyword": "recovered"}, timeout=1)["keyword"] == "recovered"


@pytest.mark.parametrize("bad", ["fail", "hang", "none", "empty"])
def test_real_plugin_search_keeps_partial_results_and_searches_each_plugin_once(tmp_path, monkeypatch, bad):
    root = Path(__file__).resolve().parents[2]
    bridge = tmp_path / "bridge"
    shutil.copytree(root / "app/music/online/js_bridge", bridge, ignore=shutil.ignore_patterns("node_modules"))
    f = bridge / "bridge.js"
    f.write_text(f.read_text("utf8").replace("timeoutMs = 8000", "timeoutMs = 100"), "utf8")
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    good_result = "[]" if bad == "empty" else "[{id:String(++n),title:'generated'}]"
    (plugins / "z-good.js").write_text("let n=0;module.exports={platform:'good',search:async()=>{console.log('plugin-noise');return "+good_result+"}}", "utf8")
    if bad not in ("none", "empty"):
        body = "throw Error('fixture')" if bad == "fail" else "return new Promise(()=>{})"
        (plugins / "a-bad.js").write_text("module.exports={platform:'bad',search:async()=>{"+body+"}}", "utf8")
    monkeypatch.setenv("NODE_EXE", str(NODE))
    src = JSMusicSource(str(plugins), str(tmp_path / "empty"), bridge_dir=str(bridge))
    try:
        result = src._call("search", {"source": "*", "keyword": "generated", "limit": 5}, timeout=1)
        assert result["successful"] == 1
        if bad == "empty":
            assert result["tracks"] == []
        else:
            assert len(result["tracks"]) == 1 and result["tracks"][0]["track_id"] == "1"
        assert result["errors"] == ({} if bad in ("none", "empty") else {"bad": "timeout" if bad == "hang" else "plugin"})
    finally:
        src.close()


def test_actual_bridge_console_output_is_stderr_and_stdout_is_only_json(tmp_path):
    plugin = tmp_path / "fixture.js"
    plugin.write_text("console.log('fixture-load');module.exports={platform:'fixture',search:async()=>{console.log('fixture-search');return []}}", "utf8")
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    result = subprocess.run([str(NODE), str(bridge)], env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR=""),
                            input='{"id":"test","method":"search","params":{"source":"*","keyword":"generated"}}\n',
                            capture_output=True, text=True, timeout=3)
    assert result.returncode == 0
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert [f["id"] for f in frames] == ["_ready", "test"]
    assert frames[1]["result"] == {"tracks": [], "errors": {}, "successful": 1}
    assert "fixture-load" in result.stderr and "fixture-search" in result.stderr
