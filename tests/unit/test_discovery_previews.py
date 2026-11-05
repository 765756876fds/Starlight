import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from app.movie.discovery import BilibiliRank, MovieDiscovery, merge_works


def test_preview_limits_each_kind_but_caches_the_actual_unpaginated_response(tmp_path):
    calls = []
    def read(kind):
        calls.append(kind)
        return [dict(title=f"{kind}{i}", kind=kind, year=None, platform="bilibili", platform_name="B站",
                     rank=i + 1, source_id=str(i), url="https://www.bilibili.com/bangumi/play/ss1") for i in range(100)]
    provider = SimpleNamespace(key="bilibili", name="B站", categories=("movie", "tv", "anime"), read=read)
    service = MovieDiscovery(tmp_path / "cache.json", [provider])
    home = service.read("all", limit=6)
    assert len(home["works"]) == 18
    assert home["totals"] == {"movie": 100, "tv": 100, "anime": 100, "variety": 0}
    assert sorted(calls) == ["anime", "movie", "tv"]
    assert len(service.read("tv")["works"]) == 100
    assert len(calls) == 3, "A category can reuse its full cached response without another HTTP request"
    service.read("movie", force=True)
    assert calls[-1] == "movie" and len(calls) == 4
    assert service.read("variety")["works"] == [] and len(calls) == 4


@pytest.mark.parametrize("limit", [0, -1, 101, True, 1.5])
def test_invalid_preview_limits_are_rejected(tmp_path, limit):
    with pytest.raises(ValueError):
        MovieDiscovery(tmp_path / "cache.json", []).read(limit=limit)


def test_variety_same_season_can_merge_but_other_and_unknown_seasons_cannot():
    base = dict(title="节目", kind="variety", year=2024, region="中国", season=1,
                platform="bilibili", platform_name="B站", rank=1)
    another = dict(base, platform="iqiyi", platform_name="爱奇艺", rank=3)
    assert len(merge_works([base, another])) == 1
    assert len(merge_works([base, dict(another, season=2)])) == 2
    assert len(merge_works([base, dict(another, season=None)])) == 2


@pytest.mark.parametrize("url,expected", [
    ("//i0.hdslb.com/a.jpg", "https://i0.hdslb.com/a.jpg"),
    ("https://i1.hdslb.com/a.png", "https://i1.hdslb.com/a.png"),
    ("file:///private/a.jpg", ""), ("https://hdslb.com.evil.example/a", ""), (None, "")])
def test_movie_posters_preserve_only_actual_allowed_public_image_urls(url, expected):
    assert BilibiliRank.cover_url(url) == expected


def test_node_artist_and_album_browse_reuses_plugin_exports_and_keeps_raw_items(tmp_path):
    node = shutil.which("node")
    assert node, "Node required for real bridge protocol verification"
    (tmp_path / "artist.js").write_text('''module.exports={platform:'demo', marker:'original',
search:async function(query,page,type){if(this.marker!=='original')throw Error('receiver');return {data:[{id:type,title:query}],isEnd:true}},
getArtistWorks:async function(item,page,type){if(type!=='music'||item.id!=='artist')throw Error('params');return {data:[{id:'song',title:'live version',album:{name:'Live'}}],isEnd:true}},
getAlbumInfo:async function(item,page){if(item.id!=='album')throw Error('params');return {musicList:[{id:'album-song',title:'studio version'}],isEnd:true}}};''', encoding="utf8")
    requests = [
        {"id": "artist", "method": "browse", "params": {"source": "demo", "operation": "artists", "kind": "artist", "keyword": "歌手", "page": 1}},
        {"id": "album", "method": "browse", "params": {"source": "demo", "operation": "artists", "kind": "album", "keyword": "专辑", "page": 1}},
        {"id": "tracks", "method": "browse", "params": {"source": "demo", "operation": "artist_tracks", "item": {"id": "artist"}}},
        {"id": "album-tracks", "method": "browse", "params": {"source": "demo", "operation": "album_tracks", "item": {"id": "album"}}},
        {"id": "empty", "method": "browse", "params": {"source": "demo", "operation": "artists", "keyword": ""}},
    ]
    bridge = Path(__file__).resolve().parents[2] / "app/music/online/js_bridge/bridge.js"
    run = subprocess.run([node, str(bridge)], input="\n".join(json.dumps(row) for row in requests) + "\n",
                         capture_output=True, text=True, encoding="utf8", timeout=8,
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                         env=dict(os.environ, MF_PLUGIN_DIR=str(tmp_path), LX_PLUGIN_DIR="", MF_USER_VARIABLES="{}"))
    assert run.returncode == 0, run.stderr
    out = {row["id"]: row for row in map(json.loads, run.stdout.splitlines())}
    assert all(out[key]["ok"] for key in ("artist", "album", "tracks", "album-tracks"))
    assert out["artist"]["result"]["data"][0]["id"] == "artist"
    assert out["album"]["result"]["data"][0]["id"] == "album"
    assert out["tracks"]["result"]["tracks"][0]["album"] == "Live"
    assert out["tracks"]["result"]["tracks"][0]["extra"]["_raw"]["id"] == "song"
    assert out["album-tracks"]["result"]["tracks"][0]["source_name"] == "demo"
    assert out["empty"]["ok"] is False
