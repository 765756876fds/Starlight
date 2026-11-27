"""Protocol tests. Windows token and broker behavior are checked separately."""

import json
import os
import queue
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

import pytest

from app.music.online.isolated_host.source import IsolatedMusicSource
from app.music.online.models import OnlineTrack
from app.music.online.source import SourceSearchError


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = ROOT / "app/music/online/isolated_host/bootstrap.js"
ORIGIN = "https://fixture.invalid"


@contextmanager
def _protocol(tmp_path, script, *, protocol="musicfree", providers=()):
    bundled = ROOT / "third_party/node/node.exe"
    node = str(bundled) if bundled.exists() else shutil.which("node")
    if node is None:
        pytest.skip("Node runtime is required for isolated plugin protocol tests")
    package = tmp_path / "package"
    package.mkdir()
    (package / "plugin.js").write_text(script, encoding="utf-8")
    (package / "fixture.json").write_text('{"tag":"verified"}', encoding="utf-8")
    (package / "unlisted.json").write_text('{"tag":"unlisted"}', encoding="utf-8")
    manifest = tmp_path / "verified.json"
    manifest.write_text(json.dumps(["plugin.js", "fixture.json"]), encoding="utf-8")
    base_env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR") if key in os.environ}
    env = dict(base_env, STARLIGHT_PLUGIN_PATH=str(package / "plugin.js"),
               STARLIGHT_PLUGIN_ROOT=str(package), STARLIGHT_PLUGIN_FILES=str(manifest),
               STARLIGHT_PLUGIN_ID="reviewed-plugin", STARLIGHT_PLUGIN_TYPE=protocol,
               STARLIGHT_SOURCE_ID="source-catalog", STARLIGHT_PLUGIN_ORIGIN=ORIGIN,
               STARLIGHT_LX_SOURCES=json.dumps(providers), STARLIGHT_PLUGIN_CONFIG="{}")
    process = subprocess.Popen([node, str(BOOTSTRAP)], cwd=tmp_path, env=env, stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, encoding="utf-8")
    frames = queue.Queue()

    def read():
        for line in process.stdout:
            frames.put(json.loads(line))
        frames.put(None)

    reader = Thread(target=read, daemon=True)
    reader.start()

    def send(frame):
        process.stdin.write(json.dumps({**frame, "v": 1}) + "\n")
        process.stdin.flush()

    def receive():
        frame = frames.get(timeout=5)
        assert frame is not None, "Node bootstrap exited before replying"
        return frame

    try:
        yield send, receive
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)
        reader.join(timeout=2)
        process.stdout.close()


def _send_call(send, sequence, method, **params):
    call_id = f"{'a' * 32}:{sequence}"
    send({"type": "call", "id": call_id, "method": method, "params": params})
    return call_id


def _receive_result(receive, call_id):
    frame = receive()
    assert frame["type"] == "result" and frame["id"] == call_id and frame["ok"], frame
    return frame["result"]


def test_musicfree_pages_direct_media_and_metadata(tmp_path):
    script = """
const fixture = require('./fixture.json');
let unlistedBlocked = false;
try { require('./unlisted.json'); } catch (error) { unlistedBlocked = error.message === 'REQUIRE_UNAVAILABLE'; }
module.exports = {
    search: (keyword, page, type) => ({isEnd: page === 3, total: 3, data: [{
        id: 'track-' + page,
        title: `${keyword} ${fixture.tag} ${page} ${type} ${unlistedBlocked}`,
        duration: page === 1 ? 10001 : undefined,
        durationMs: page === 2 ? 60000 : undefined,
        url: page === 3 ? 'file:///test-not-real/media.wav' : hostConfig.origin + '/audio',
        cover: hostConfig.origin + '/cover',
        qualities: {high: {sizeBytes: 123, format: 'mp3'}},
    }]}),
    getLyric: _ => '[00:01.00]controlled lyric',
};
"""
    with _protocol(tmp_path, script) as (send, receive):
        assert receive()["capabilities"]["resolve_media"] is False
        track = []
        for page in (1, 2, 3):
            call_id = _send_call(send, page, "searchPage", source="source-catalog", keyword="Fixture", page=page)
            result = _receive_result(receive, call_id)
            assert (result["page"], result["is_end"], result["total"]) == (page, page == 3, 3)
            track.append(result["tracks"][0])
        assert [row["track_id"] for row in track] == ["track-1", "track-2", "track-3"]
        assert [row["duration_seconds"] for row in track] == [10001, 60, 0]
        raw = track[0]["extra"]["_raw"]
        assert raw["qualities"] == {"high": {"sizeBytes": 123, "format": "mp3"}}
        assert "url" not in raw and "cover" not in raw
        assert track[0]["title"] == "Fixture verified 1 music true"
        result = _receive_result(receive, _send_call(send, 4, "downloadUrl", track_id="track-1", quality=None))
        assert result == {"url": f"{ORIGIN}/audio", "supports_renditions": False, "direct_url": True}
        assert _receive_result(receive, _send_call(send, 5, "downloadUrl", track_id="track-1", quality="super")) is None
        assert _receive_result(receive, _send_call(send, 6, "getUrl", track_id="track-3")) is None
        assert _receive_result(receive, _send_call(send, 7, "getCover", track_id="track-2"))["url"] == ORIGIN + "/cover"
        assert _receive_result(receive, _send_call(send, 8, "getLyric", track_id="track-2"))["lyric"] == (
            "[00:01.00]controlled lyric")


def test_lx_provider_scope_host_request_and_exact_tier(tmp_path):
    script = """
lx.on(lx.EVENT_NAMES.request, async data => {
    if (data.action === 'lyric') return hostConfig.origin + '/lyric';
    if (data.info.musicInfo.id !== 17) return null;
    const response = await lx.request(hostConfig.origin + '/manifest');
    return {
        url: response.body.url,
        quality: data.info.type,
        headers: {'X-Fixture': 'accepted', Host: 'ignored.invalid'},
    };
});
lx.send(lx.EVENT_NAMES.inited, {sources: {
    kw: {actions: ['musicUrl', 'lyric'], qualitys: ['flac', '320k']},
    mg: {actions: ['musicUrl'], qualitys: ['flac24bit']},
}});
"""
    with _protocol(tmp_path, script, protocol="lx", providers=("kw",)) as (send, receive):
        capabilities = receive()["capabilities"]
        assert capabilities["search"] is False and capabilities["provider_qualities"] == {"kw": ["flac", "320k"]}
        raw = {"musicInfo": {"id": 17, "source": "kw"}}
        call_id = _send_call(send, 1, "downloadUrl", track_id="17", raw_item=raw, quality="flac")
        request = receive()
        assert request["type"] == "host_request" and request["call_id"] == call_id
        assert request["request"]["url"] == ORIGIN + "/manifest"
        send({"type": "host_response", "call_id": call_id, "id": request["id"], "ok": True,
              "result": {"status": 200, "headers": {"content-type": "application/json"},
                         "body": json.dumps({"url": ORIGIN + "/audio"})}})
        result = _receive_result(receive, call_id)
        assert result["source_quality"] == "flac" and result["headers"] == {"x-fixture": "accepted"}
        other = {"musicInfo": {"id": 17, "source": "mg"}}
        assert _receive_result(receive, _send_call(send, 2, "getUrl", track_id="17", raw_item=other)) is None
        assert _receive_result(receive, _send_call(send, 3, "downloadUrl", track_id="17", raw_item=raw,
                                                   quality="flac24bit")) is None
        assert _receive_result(receive, _send_call(send, 4, "getLyric", track_id="17", raw_item=raw))["url"] == (
            ORIGIN + "/lyric")


def test_python_source_snapshot_uses_no_rpc_and_rejects_foreign_rows_after_close():
    calls = []

    def invoke(method, params, *, timeout):
        calls.append((method, params, timeout))
        return {"status": "ok", "source": "owned", "page": params["page"], "is_end": False, "total": 8,
                "tracks": [{"source_name": "other", "track_id": "forged"},
                           {"source_name": "owned", "track_id": "raw-123", "title": "Fixture",
                            "extra": {"_raw": {"id": "raw-123", "url": ORIGIN + "/signed"}}}]}

    source = IsolatedMusicSource("owned", "musicfree", (), invoke)
    token, aliases = source.search_snapshot()
    assert aliases == ("owned",) and not calls
    page = source.search_page(token, "owned", "word", 2)
    assert page.is_end is False and page.total == 8
    assert page.tracks == [OnlineTrack("owned", "raw-123", "Fixture", extra={"_raw": {"id": "raw-123"}})]
    assert source.get_play_url(OnlineTrack("other", "raw-123", "Fixture")) is None
    source.close()
    with pytest.raises(SourceSearchError, match="unavailable"):
        source.search_page(token, "owned", "word", 3)
    assert [method for method, _, _ in calls] == ["searchPage"]


@pytest.mark.parametrize(("code", "category"), [
    ("isolated_source_unavailable", "unavailable"),
    ("isolated_host_timeout", "timeout"),
    ("isolated_source_revoked", "interrupted"),
    ("isolated_host_busy", "busy"),
    ("isolated_source_busy", "busy"),
    ("isolated_cleanup_pending", "busy"),
])
def test_python_source_preserves_host_failure_categories(code, category):
    def error(*args, **kwargs):
        raise RuntimeError(code)

    source = IsolatedMusicSource("owned", "musicfree", (), error)
    token, _ = source.search_snapshot()
    with pytest.raises(SourceSearchError, match=category):
        source.search_page(token, "owned", "word", 2)
