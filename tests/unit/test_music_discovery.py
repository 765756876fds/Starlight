import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock

import pytest

from app.music.online.manager import OnlineSourceManager
from app.music.online.models import CatalogResults, OnlineTrack
from app.music.online.source import SourceSearchError
from app.music.online.sources.js_source import JSMusicSource
from app.ui.panels.music_panel import _RankDetailWorker, _RankListsWorker


def test_catalog_preserves_partial_and_total_failures_without_fake_empty_success():
    manager = OnlineSourceManager()
    good = Mock(name="good")
    good.name = "good"
    value = CatalogResults()
    value["test-source"] = [{"title": "新歌", "lists": [{"id": "1", "title": "新歌榜"}]}]
    value.errors["inner-bad"] = "plugin"
    good.get_top_lists.return_value = value
    bad = Mock()
    bad.name = "bad"
    bad.get_top_lists.side_effect = SourceSearchError("timeout")
    manager.register(good)
    manager.register(bad)
    try:
        result = manager.get_top_lists()
        assert result["test-source"] == value["test-source"]
        assert result.errors == {"inner-bad": "plugin", "bad": "timeout"}
        manager.unregister("good")
        result = manager.get_top_lists()
        assert not result and result.errors == {"bad": "timeout"}
    finally:
        manager.close()


def test_source_browse_parses_real_online_track_and_does_not_use_search():
    source = JSMusicSource.__new__(JSMusicSource)
    source._call = Mock(return_value={"tracks": [{"title": "现场版", "artist": "歌手", "album": "演唱会",
                                                "source_name": "source", "track_id": "version-2"}], "is_end": True})
    result = source.browse("source", "sheet", item={"id": "sheet"})
    assert isinstance(result["tracks"][0], OnlineTrack)
    assert result["tracks"][0].album == "演唱会" and result["tracks"][0].track_id == "version-2"
    assert source._call.call_args.args[0] == "browse"


def test_source_catalog_supports_old_array_and_new_partial_result_contract():
    source = JSMusicSource.__new__(JSMusicSource)
    group = {"source": "source", "groups": [{"title": "热歌", "lists": [{"id": "1"}]}]}
    source._call = Mock(return_value=[group])
    assert source.get_top_lists() == {"source": group["groups"]}
    source._call.return_value = {"groups": [group], "errors": {"another": "plugin"}}
    assert source.get_top_lists().errors == {"another": "plugin"}
    source._call.return_value = None
    with pytest.raises(SourceSearchError):
        source.get_top_lists()


def test_rank_worker_retains_source_failure_metadata_and_unknown_details_fail(qapp):
    result = CatalogResults()
    result.errors["source"] = "timeout"
    worker = _RankListsWorker(Mock(get_top_lists=lambda: result))
    received = []
    worker.done.connect(received.append)
    worker.run()
    assert received[0] is result
    detail = _RankDetailWorker(Mock(get_top_list_detail=lambda *_: None), "source", "1")
    finished, failed = [], []
    detail.done.connect(finished.append)
    detail.failed.connect(failed.append)
    detail.run()
    assert not finished and failed


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


def test_actual_node_plugin_discovery_and_bridge_protocol(tmp_path):
    node = shutil.which("node")
    assert node, "Node is required to verify the actual bridge"
    plugin = tmp_path / "demo.js"
    plugin.write_text('''module.exports={platform:'demo',marker:'original-export',
getTopLists:async()=>[{title:'new',data:[{id:'chart',title:'new music'}]}],
getTopListDetail:async()=>({musicList:[],isEnd:true}),
getRecommendSheetTags:async()=>({pinned:[{id:'calm',title:'Calm'}]}),
getRecommendSheetsByTag:async function(tag,page){if(this.marker!=='original-export')throw Error('wrong receiver');return {data:[{id:'s1',title:tag.title,album:'album'}],isEnd:page>1}},
getMusicSheetInfo:async function(item,page){if(this.marker!=='original-export')throw Error('wrong receiver');return {musicList:[{id:'t1',title:'live',artists:[{name:'artist'}],album:null},{id:'t2',title:'studio',album:{name:'named album'}}],isEnd:true}}};''', encoding="utf-8")
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    requests = [
        {"id": "tags", "method": "browse", "params": {"source": "demo", "operation": "tags"}},
        {"id": "sheets", "method": "browse", "params": {"source": "demo", "operation": "sheets", "tag": {"id": "calm", "title": "Calm"}, "page": 1}},
        {"id": "tracks", "method": "browse", "params": {"source": "demo", "operation": "sheet", "item": {"id": "s1"}}},
        {"id": "charts", "method": "getTopLists", "params": {}},
    ]
    process = subprocess.run([node, str(bridge)], input="\n".join(json.dumps(r) for r in requests) + "\n",
                             capture_output=True, text=True, encoding="utf-8", timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR="", MF_USER_VARIABLES="{}"))
    assert process.returncode == 0, process.stderr
    replies = {r["id"]: r for r in map(json.loads, process.stdout.splitlines())}
    assert replies["_ready"]["plugins"][0]["capabilities"]["sheets"] is True
    assert all(replies[key]["ok"] for key in ("tags", "sheets", "tracks", "charts"))
    assert replies["sheets"]["result"]["is_end"] is False
    assert replies["tracks"]["result"]["tracks"][0]["album"] == ""
    assert replies["tracks"]["result"]["tracks"][1]["album"] == "named album"
    assert replies["tracks"]["result"]["tracks"][0]["source_name"] == "demo"
    assert replies["charts"]["result"]["groups"][0]["source"] == "demo"


def test_actual_bridge_returns_other_charts_when_one_plugin_never_finishes(tmp_path):
    node = shutil.which("node")
    assert node, "Node is required to verify plugin timeout isolation"
    (tmp_path / "good.js").write_text("module.exports={platform:'good',getTopLists:async()=>[{title:'ok',data:[{id:'1',title:'榜单'}]}]};", encoding="utf8")
    (tmp_path / "hung.js").write_text("module.exports={platform:'hung',getTopLists:()=>new Promise(()=>{})};", encoding="utf8")
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    request = {"id": "bounded", "method": "getTopLists", "params": {}}
    process = subprocess.run([node, str(bridge)], input=json.dumps(request) + "\n",
                             capture_output=True, text=True, encoding="utf8", timeout=11,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                             env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR="", MF_USER_VARIABLES="{}"))
    assert process.returncode == 0, process.stderr
    replies = {r["id"]: r for r in map(json.loads, process.stdout.splitlines())}
    result = replies["bounded"]["result"]
    assert [g["source"] for g in result["groups"]] == ["good"]
    assert result["errors"] == {"hung": "timeout"}
