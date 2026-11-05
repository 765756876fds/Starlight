"""Reproducible catalogue races and actual Node bridge error contracts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from types import SimpleNamespace

from app.movie.discovery import MovieDiscovery


def test_older_refresh_cannot_replace_newer_movie_cache(tmp_path):
    started, release = threading.Event(), threading.Event()
    calls = []

    def read(kind):
        number = len(calls)
        calls.append(number)
        if number == 0:
            started.set()
            assert release.wait(2)
        return [dict(title="old" if number == 0 else "new", kind=kind, rank=1)]

    provider = SimpleNamespace(key="demo", name="demo", categories=["movie"], read=read)
    service = MovieDiscovery(tmp_path / "ranks.json", [provider])
    result, errors = [], []

    def slow():
        try:
            result.append(service.read("movie", force=True))
        except Exception as exc:
            errors.append(exc)

    worker = threading.Thread(target=slow)
    worker.start()
    try:
        assert started.wait(1)
        assert service.read("movie", force=True)["works"][0]["title"] == "new"
    finally:
        release.set()
        worker.join(3)
    assert not worker.is_alive() and not errors
    assert result[0]["works"][0]["title"] == "old", "Already issued operations are not cancelled"
    assert service.read("movie")["works"][0]["title"] == "new"
    assert MovieDiscovery(tmp_path / "ranks.json", [provider]).read("movie")["works"][0]["title"] == "new"


def test_real_bridge_rejects_unknown_chart_result_and_allows_immediate_retry(tmp_path):
    node = shutil.which("node")
    assert node, "Use the real Node protocol, not a synchronous mock"
    (tmp_path / "charts.js").write_text("""let calls=0;
module.exports={platform:'demo',getTopLists:async()=>[],
getTopListDetail:async function(top){
 if(top.id==='bad-schema')return {};
 if(top.id==='empty')return {musicList:[],isEnd:true};
 if(++calls===1)return null;
 return {musicList:[{id:'1',title:'actual song'}],isEnd:true};
}};""", encoding="utf-8")
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    requests = [dict(id=str(i), method="getTopListDetail", params=dict(source="demo", list_id=value))
                for i, value in enumerate(["retry", "retry", "empty", "bad-schema"])]
    run = subprocess.run([node, str(bridge)], input="\n".join(json.dumps(row) for row in requests) + "\n",
                         capture_output=True, text=True, encoding="utf-8", timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR="", MF_USER_VARIABLES="{}"))
    assert run.returncode == 0, run.stderr
    rows = {row["id"]: row for row in map(json.loads, run.stdout.splitlines())}
    assert rows["0"]["ok"] is False, "Unknown response is not a successful empty chart"
    assert rows["1"]["ok"] and rows["1"]["result"]["tracks"][0]["title"] == "actual song"
    assert rows["2"]["ok"] and rows["2"]["result"] == {"is_end": True, "tracks": []}
    assert rows["3"]["ok"] is False


def test_real_bridge_keeps_invalid_directory_failure_separate_from_empty_directory(tmp_path):
    node = shutil.which("node")
    assert node
    (tmp_path / "broken.js").write_text("module.exports={platform:'broken',getTopLists:async()=>null};", encoding="utf-8")
    (tmp_path / "empty.js").write_text("module.exports={platform:'empty',getTopLists:async()=>[]};", encoding="utf-8")
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    run = subprocess.run([node, str(bridge)], input=json.dumps(dict(id="catalog", method="getTopLists", params={})) + "\n",
                         capture_output=True, text=True, encoding="utf-8", timeout=10,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR="", MF_USER_VARIABLES="{}"))
    assert run.returncode == 0, run.stderr
    rows = {row["id"]: row for row in map(json.loads, run.stdout.splitlines())}
    assert rows["catalog"]["ok"] is True, "Independent plugins still return a usable partial response"
    assert rows["catalog"]["result"] == {"groups": [], "errors": {"broken": "plugin"}}
