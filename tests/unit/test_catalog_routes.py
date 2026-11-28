from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import pytest

from app.music.download_journal import restore_song, song_record
from app.music.online.catalog_routes import CatalogRoute, resolver_track, validate_routes
from app.music.online.isolated_host.settings import IsolatedSourceSettings
from app.music.online.manager import OnlineSourceManager
from app.music.online.models import OnlineTrack
from app.music.online.source_authority import DownloadTransportError, ResourcePolicy, SourceAuthority, TransferBudget


CATALOG = "https://catalog.example"
RESOLVER = "https://resolver.example"


class Source:
    def __init__(self, name, role, platforms=(), url=None):
        self.name, self.plugin_type, self.providers = name, role, platforms
        self.url = url
        self.calls = []

    def search(self, query, limit):
        return [OnlineTrack(self.name, "catalog-13", "Track", "Artist", "Album", 181,
                            extra={"_raw": {"source": "tx", "songmid": "native-42", "songId": 42,
                                           "isrc": "USABC2301234", "url": CATALOG + "/extra"},
                                   "_catalog": {"untrusted": "marker"}})]

    def get_playback_rendition(self, track, preference):
        self.calls.append(track)
        return {"url": self.url} if self.url else None

    def get_download_rendition(self, track, native, timeout):
        return self.get_playback_rendition(track, native)

    def close(self):
        pass


@pytest.fixture
def registry():
    route = CatalogRoute("catalog", "tx", ("r1", "r2"))
    manager = OnlineSourceManager(allow_legacy_sources=False, catalog_routes=(route,))
    authority = SourceAuthority()
    sources = (Source("catalog", "musicfree"), Source("r1", "lx", ("tx",), CATALOG + "/unauthorized"),
               Source("r2", "lx", ("tx",), RESOLVER + "/audio"))
    policies = {role: ResourcePolicy((RESOLVER,), TransferBudget(2, 64))
                for role in ("download", "playback")}
    for source in sources:
        manager.register(source, lease=authority.bind(source.name, source.name, 1, policies))
    try:
        yield manager, authority, sources, policies
    finally:
        manager.close()


def test_lookup_uses_native_id_and_both_live_authorities(registry):
    manager, _, sources, _ = registry
    track, = manager.search_all("Track")["catalog"]
    assert "_catalog" not in track.extra
    candidates = manager.catalog_candidates(track)
    assert [(item.source_name, item.track_id) for item in candidates] == [
        ("r1", "catalog:tx:native-42"), ("r2", "catalog:tx:native-42")]
    assert candidates[0].extra["_raw"]["musicInfo"]["songId"] == 42
    assert "url" not in candidates[0].extra["_raw"]
    assert candidates[0].extra["_catalog"]["source"] == "catalog"
    grant = manager.get_playback_url(track, "auto")["_authorization"]
    assert grant.lease.plugin_id == "r2" and sources[1].calls and sources[2].calls
    grant.check_owner("catalog", "catalog-13")
    with pytest.raises(DownloadTransportError):
        grant.check_owner("catalog", "different")
    record = restore_song(song_record(vars(candidates[1])))
    selected = OnlineTrack(**record)
    download = manager.get_download_rendition(selected, "flac")["_authorization"]
    download.check_owner("r2", "catalog:tx:native-42")
    with pytest.raises(DownloadTransportError):
        download.check_owner("catalog", "catalog-13")
    with grant.slot():
        assert grant.lease.busy()
        assert manager._managed_owner(track)[1].busy()
    manager.unregister("catalog")
    assert not grant.active() and not download.active()
    assert manager.get_download_rendition(selected, "flac") is None


def test_wrong_platform_invalid_provenance_and_variants_never_alias(registry):
    manager, _, _, _ = registry
    original, = manager.search_all("Track")["catalog"]
    forged = replace(original, extra={"_raw": {"source": "kw", "songmid": "native-42"}})
    assert manager.catalog_candidates(forged) == ()
    candidate = manager.catalog_candidates(original)[1]
    altered = replace(candidate, track_id="kw:native-42")
    assert manager.get_download_rendition(altered, "flac") is None
    kg = replace(original, extra={"_raw": {"source": "kg", "songmid": 13, "hash": "mainhash"}})
    other = replace(kg, extra={"_raw": {"source": "kg", "songmid": 13, "hash": "otherhash"}})
    assert resolver_track(kg, "kg", "r2").track_id == "catalog:kg:13:mainhash"
    assert resolver_track(other, "kg", "r2").track_id == "catalog:kg:13:otherhash"
    assert resolver_track(kg, "tx", "r2") is None
    assert resolver_track(replace(kg, extra={"_raw": {"songmid": 13}}), "kg", "r2") is None


def test_late_resolution_does_not_rebind_to_new_catalog_lease(registry):
    manager, authority, sources, policies = registry
    track, = manager.search_all("Track")["catalog"]
    entered, release = Event(), Event()

    def blocked(item, preference):
        entered.set()
        assert release.wait(3)
        return {"url": sources[2].url}

    sources[1].get_playback_rendition = blocked
    with ThreadPoolExecutor(1) as worker:
        result = worker.submit(manager.get_playback_url, track, "auto")
        try:
            assert entered.wait(3)
            lease = authority.bind("catalog", "catalog", 1, policies)
            manager.register(sources[0], lease=lease)
        finally:
            release.set()
        assert result.result(timeout=3) is None
    assert not sources[2].calls


def test_route_settings_require_supported_resolver_and_managed_registry():
    routes = (CatalogRoute("catalog", "tx", ("r1",)),)
    with pytest.raises(ValueError, match="managed"):
        OnlineSourceManager(catalog_routes=routes)
    settings = (IsolatedSourceSettings("catalog", "musicfree", (), {}),
                IsolatedSourceSettings("r1", "lx", ("kw",), {}))
    with pytest.raises(ValueError, match="invalid_catalog_resolver"):
        validate_routes(routes, settings)


def test_lx_tier_hash_is_delivered_as_declaration_after_journal_restore():
    from app.music.online.isolated_host.source import IsolatedMusicSource

    raw = {"source": "kg", "songmid": 13, "hash": "mainhash",
           "qualities": {"flac": {"hash": "tierhash", "sizeBytes": 4321,
                                  "url": "https://resolver.example/untrusted"}}}
    track = OnlineTrack("catalog", "local-id", "Title", authorization_plugin="catalog", extra={"_raw": raw})
    routed = resolver_track(track, "kg", "r2")
    restarted = OnlineTrack(**restore_song(song_record(vars(routed))))
    adapter = IsolatedMusicSource("r2", "lx", ("kg",), lambda *_args, **_kw: None)
    params = adapter._track_params(restarted)
    assert params["track_id"] == "13"
    info = params["raw_item"]["musicInfo"]
    assert info["hash"] == "mainhash"
    assert info["_types"] == {"flac": {"hash": "tierhash", "sizeBytes": 4321}}
    assert info["types"] == [{"type": "flac", "hash": "tierhash", "sizeBytes": 4321}]
