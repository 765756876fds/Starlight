"""No default fallback or test-only origin grants in the explicit launch path."""
from pathlib import Path
import hashlib
import json

import pytest

from app.music.online.isolated_host.settings import load_isolated_music_settings
from app.music.online.manager import OnlineSourceManager
from app.music.online.source_authority import DownloadTransportError, ResourcePolicy, SourceAuthority, TransferBudget


def test_restricted_registry_cannot_import_unmanaged_sources(tmp_path):
    (tmp_path / "legacy.py").write_text("raise AssertionError('unexpected import')", encoding="utf-8")
    manager = OnlineSourceManager(allow_legacy_sources=False)

    class Source:
        name = "approved"

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    source = Source()
    policy = {"host_http": ResourcePolicy(("https://example.com",), TransferBudget(1, 64))}
    authority = SourceAuthority()
    first = authority.bind(source.name, source.name, 1, policy)
    try:
        assert manager.load_custom_sources(str(tmp_path)) == 0
        with pytest.raises(DownloadTransportError, match="source_scope"):
            manager.register(source)
        manager.register(source, lease=first)
        first.retire()
        second = authority.bind(source.name, source.name, 1, policy)
        assert manager.renew_source_lease(source, first, second)
        assert manager.get_source(source.name) is source
        assert not source.closed
        manager.unregister(source.name)
        third = authority.bind(source.name, source.name, 1, policy)
        try:
            assert not manager.renew_source_lease(source, second, third)
            assert manager.get_source(source.name) is None
        finally:
            third.retire()
    finally:
        manager.close()


def _write_settings(root: Path):
    raw = {"revision": 1, "publishers": {}, "plugins": {}, "revoked_publishers": []}
    trust = root / "policy.json"
    data = json.dumps(raw).encode()
    trust.write_bytes(data)
    options = {"work_parent": str(root), "trust_root": str(root / "store"), "node": str(root / "node.exe"),
               "trust_policy": str(trust), "trust_policy_sha256": hashlib.sha256(data).hexdigest(),
               "sources": [{"plugin": "unapproved", "type": "musicfree", "providers": [],
                            "policies": {"host_http": {"origins": ["https://example.com"],
                                                      "timeout": 1, "body_bytes": 64}}}]}
    return root / "host.json", options


def test_settings_requires_owner_trust_grant(tmp_path):
    path, options = _write_settings(tmp_path)
    path.write_text(json.dumps(options), encoding="utf-8")
    with pytest.raises(ValueError, match="isolated_host_plugin"):
        load_isolated_music_settings(str(path))


def test_settings_rejects_cli_loopback_exception(tmp_path):
    path, options = _write_settings(tmp_path)
    options["test_loopback_origins"] = ["http://127.0.0.1:9000"]
    path.write_text(json.dumps(options), encoding="utf-8")
    with pytest.raises(ValueError, match="isolated_host_settings"):
        load_isolated_music_settings(str(path))


def test_online_playback_endpoint_is_not_a_local_cover_path():
    from app.ui.widgets.music_card_delegate import cover_key_of

    assert cover_key_of({"path": "http://127.0.0.1:8123/ephemeral"}) is None
    assert cover_key_of({"path": "C:/Music/local.flac"}) == "local:C:/Music/local.flac"
