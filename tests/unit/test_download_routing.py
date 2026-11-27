"""Exact download resolution. Fixtures never connect to a media host."""
import shutil
import time

import pytest

from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from app.music.online.sources.js_source import JSMusicSource


class UntieredSource:
    def __init__(self, name, reply=None):
        self.name = name
        self.reply = reply
        self.ids = []

    def get_play_url(self, track):
        self.ids.append(track.track_id)
        return self.reply

    def close(self):
        pass


def test_untiered_download_does_not_send_foreign_native_id():
    manager = OnlineSourceManager()
    owner = UntieredSource("owner")
    foreign = UntieredSource("foreign", "https://media.invalid/foreign")
    manager.register(owner)
    manager.register(foreign)
    try:
        track = OnlineTrack("owner", "shared", "title")
        missing = OnlineTrack("unknown", "shared", "title")
        assert manager.download_tiers(track) == ((None, None),)
        assert not manager.download_tiers(missing)
        assert manager.get_download_rendition(track, "super") is None
        assert manager.get_download_rendition(track, None) is None
        assert manager.get_download_rendition(missing, None) is None
        assert owner.ids == ["shared"]
        assert foreign.ids == []
        owner.reply = "https://media.invalid/owned"
        assert manager.get_download_rendition(track, None) == {
            "url": owner.reply, "headers": None, "source": "owner",
            "requested_quality": None, "source_quality": None,
        }
    finally:
        manager.close()


@pytest.fixture
def plugin_manager(tmp_path, monkeypatch):
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js not on PATH")
    monkeypatch.setenv("NODE_EXE", node)
    lx = tmp_path / "lx"
    mf = tmp_path / "mf"
    lx.mkdir()
    mf.mkdir()
    (mf / "owner.js").write_text("""
const fs = require('node:fs');
const path = require('node:path');
const timers = require('node:timers/promises');
module.exports = {
  platform: 'owner',
  async getMediaSource(item, tier) {
    if (tier === 'super') return null;
    if (tier === 'low') {
      fs.writeFileSync(path.join(__dirname, 'started'), '');
      for (let i = 0; i < 240 && !fs.existsSync(path.join(__dirname, 'release')); i++) await timers.setTimeout(10);
    }
    return {url:'https://media.invalid/' + item.id + '/' + tier,
            quality:'standard'};
  },
};
""", encoding="utf8")
    bridge = JSMusicSource(str(mf), str(lx))
    manager = OnlineSourceManager()
    try:
        assert bridge.available
        manager.register(bridge)
        yield manager, mf
    finally:
        manager.close()
        bridge.close()


def test_real_node_download_requests_exact_tier_and_splits_quality_evidence(plugin_manager):
    manager, _ = plugin_manager
    track = OnlineTrack("owner", "shared", "title", extra={"_raw": {"id": "shared"}})
    incompatible = OnlineTrack("owner", "shared", "title", extra={"_raw": {"id": "other"}})
    assert manager.download_tiers(track) == (
        ("super", "super"), ("high", "high"), ("standard", "standard"), ("low", "low"),
    )
    assert manager.get_download_rendition(track, "super") is None
    assert manager.get_download_rendition(incompatible, "high") is None
    assert manager.get_download_rendition(track, "high") == {
        "url": "https://media.invalid/shared/high", "headers": None,
        "source": "owner", "requested_quality": "high", "source_quality": "standard",
    }


def test_logical_timeout_does_not_free_unresolved_plugin_slot(plugin_manager):
    manager, mf = plugin_manager
    track = OnlineTrack("owner", "shared", "title")
    assert manager.get_download_rendition(track, "low", timeout=.2) is None
    assert (mf / "started").is_file()
    assert manager.get_download_rendition(track, "high") is None
    (mf / "release").touch()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        result = manager.get_download_rendition(track, "high")
        if result:
            assert result["source"] == "owner"
            break
        time.sleep(.01)
    else:
        pytest.fail("plugin slot did not free after the original request settled")


def test_legacy_url_timeout_preserves_per_owner_real_slot():
    from threading import Event

    manager = OnlineSourceManager()
    release = Event()
    entered = Event()

    class Slow(UntieredSource):
        def get_play_url(self, track):
            self.ids.append(track.track_id)
            entered.set()
            release.wait(5)
            return "https://example.invalid/resource"

    source = Slow("slow")
    manager.register(source)
    track = OnlineTrack("slow", "same", "recording")
    try:
        assert manager.get_download_rendition(track, None, timeout=.05) is None
        assert entered.is_set()
        for _ in range(20):
            assert manager.get_download_rendition(track, None, timeout=.05) is None
        assert len(source.ids) == 1
        with manager._life_lock:
            active = manager._legacy_download_active["slow"]
        release.set()
        assert active.result(timeout=1)
    finally:
        release.set()
        manager.close()
