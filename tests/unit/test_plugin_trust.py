
"""Publisher trust without running plugin code or using a production key."""
from __future__ import annotations

from base64 import b64encode
from concurrent.futures import ThreadPoolExecutor
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import threading
from zipfile import ZipFile, ZipInfo

import pytest

from app.music.online import plugin_trust as trust

_ID = "fixture-plugin"
_PUBLISHER = "fixture-publisher"
_FILES = {
    "index.js": b"module.exports = require('./lib/helper.js');",
    "lib/helper.js": b"module.exports = 'fixture';",
    "assets/default.json": b'{"enabled":true}',
}
_KEYGEN = """
const {generateKeyPairSync} = require('node:crypto');
const {publicKey, privateKey} = generateKeyPairSync('ed25519');
process.stdout.write(JSON.stringify({
  public: publicKey.export({type:'spki', format:'der'}).toString('base64'),
  private: privateKey.export({type:'pkcs8', format:'der'}).toString('base64'),
}));
"""
_SIGN = """
const {createPrivateKey, sign} = require('node:crypto');
const q = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const key = createPrivateKey({key: Buffer.from(q.key, 'base64'), format:'der', type:'pkcs8'});
process.stdout.write(sign(null, Buffer.from(q.data, 'base64'), key).toString('base64'));
"""


def _canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()


def _node(node, script, input_bytes=None):
    return subprocess.run([str(node), "-e", script], input=input_bytes, check=True,
                          capture_output=True, timeout=6).stdout


class PackageMaker:
    def __init__(self, node, runtime):
        self.node = node
        self.runtime = runtime
        self.key = json.loads(_node(node, _KEYGEN))

    def policy(self, *, revision=1, revoked=False, minimum="1.0.0"):
        raw = _canonical({
            "revision": revision,
            "publishers": {_PUBLISHER: self.key["public"]},
            "plugins": {_ID: {"publisher": _PUBLISHER, "minimum_version": minimum,
                              "runtime_sha256": self.runtime}},
            "revoked_publishers": [_PUBLISHER] if revoked else [],
        })
        return trust.TrustPolicy.from_owner_document(raw, pinned_sha256=hashlib.sha256(raw).hexdigest())

    def package(self, version="1.0.0", *, declared=None, actual=None, manifest_extra=None, signer=None):
        declared = _FILES if declared is None else declared
        actual = declared if actual is None else actual
        manifest = {"id": _ID, "publisher": _PUBLISHER, "version": version, "entry": "index.js",
                    "runtime_sha256": self.runtime,
                    "files": {name: hashlib.sha256(data).hexdigest() for name, data in declared.items()}}
        manifest.update(manifest_extra or {})
        signature = _node(self.node, _SIGN, _canonical({
            "key": (signer or self).key["private"],
            "data": b64encode(_canonical(manifest)).decode("ascii"),
        })).decode("ascii")
        manifest["signature"] = signature
        memory = BytesIO()
        with ZipFile(memory, "w") as archive:
            archive.writestr("manifest.json", _canonical(manifest))
            for name, data in actual.items():
                archive.writestr(name, data)
        return memory.getvalue()


@pytest.fixture(scope="module")
def package_maker(tmp_path_factory):
    repository = Path(__file__).resolve().parents[2]
    bundled = repository / "third_party/node/node.exe"
    system = os.environ.get("STARLIGHT_TEST_NODE") or shutil.which("node")
    source = bundled if bundled.is_file() else Path(system) if system else None
    if source is None:
        pytest.skip("Node is required to sign synthetic test fixtures")
    if os.name == "nt":
        scratch_node = tmp_path_factory.mktemp("trust-runtime") / "node.exe"
        shutil.copyfile(source, scratch_node)
    else:
        scratch_node = source.resolve()
    with scratch_node.open("rb") as f:
        pin = hashlib.file_digest(f, "sha256").hexdigest()
    return PackageMaker(scratch_node, pin)


def _review(store, bundle):
    digest = store.submit(_ID, bundle)
    store.verify_candidate(_ID, digest)
    store.review(_ID, digest)
    return digest


def _enable(store, bundle):
    return store.enable(_ID, _review(store, bundle), authorized=True)


def test_verified_dependency_bytes_and_explicit_enable(package_maker, tmp_path):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    source_archive = bytearray(maker.package())
    digest = store.submit(_ID, bytes(source_archive))
    assert store.status(_ID)["candidate_state"] == "pending_validation"
    reviewed = store.verify_candidate(_ID, digest)
    source_archive.clear()
    with pytest.raises(trust.PluginTrustError, match="not_enabled"):
        store.enabled(_ID)
    assert reviewed.files == _FILES
    assert reviewed.runtime_sha256 == maker.runtime
    with pytest.raises(TypeError):
        reviewed.files["lib/helper.js"] = b"changed"
    store.review(_ID, digest)
    assert store.status(_ID)["authorization_pending"]
    with pytest.raises(trust.PluginTrustError, match="authorization_pending"):
        store.enable(_ID, digest, authorized=False)
    assert store.enable(_ID, digest, authorized=True) == reviewed
    reopened = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    assert reopened.enabled(_ID) == reviewed


@pytest.mark.parametrize(("contents", "code"), [
    ({**_FILES, "outside.js": b"ordinary fixture"}, "file_set_mismatch"),
    ({"index.js": _FILES["index.js"]}, "file_set_mismatch"),
    ({**_FILES, "lib/helper.js": b"module.exports = 'changed fixture';"}, "file_integrity"),
    ({**_FILES, "LIB/other.js": b"ordinary fixture"}, "invalid_package_entry"),
    ({**_FILES, "index.js/data.txt": b"ordinary fixture"}, "invalid_package_entry"),
    ({**_FILES, "folder/AUX.txt": b"ordinary fixture"}, "invalid_package_entry"),
])
def test_inexact_package_is_never_eligible(package_maker, tmp_path, contents, code):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    digest = store.submit(_ID, maker.package(declared=_FILES, actual=contents))
    with pytest.raises(trust.PluginTrustError, match=code):
        store.verify_candidate(_ID, digest)
    assert store.status(_ID)["last_failure"] == "integrity_failed"
    assert store.status(_ID)["active_sha"] is None


def test_rejects_archive_link_entries_without_extracting(package_maker, tmp_path):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    archive = BytesIO()
    link = ZipInfo("lib/helper.js")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(archive, "w") as z:
        z.writestr("manifest.json", "{}")
        z.writestr(link, "fixture")
    digest = store.submit(_ID, archive.getvalue())
    with pytest.raises(trust.PluginTrustError, match="invalid_package_entry"):
        store.verify_candidate(_ID, digest)
    assert tuple(tmp_path.iterdir()) == (tmp_path / "plugin-trust.sqlite3",)


def test_host_publisher_root_cannot_be_supplied_by_plugin(package_maker, tmp_path):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    other = PackageMaker(maker.node, maker.runtime)
    cases = [(maker.package(signer=other), "signature_invalid"),
             (maker.package(signer=other, manifest_extra={"public_key": other.key["public"]}),
              "unexpected_fields")]
    for bundle, failure in cases:
        digest = store.submit(_ID, bundle)
        with pytest.raises(trust.PluginTrustError, match=failure):
            store.verify_candidate(_ID, digest)
    with pytest.raises(trust.PluginTrustError, match="policy_pin_mismatch"):
        trust.TrustPolicy.from_owner_document(b"{}", pinned_sha256="0" * 64)


def test_altered_runtime_fails_closed(package_maker, tmp_path):
    maker = package_maker
    suspect = tmp_path / maker.node.name
    shutil.copyfile(maker.node, suspect)
    with suspect.open("ab") as stream:
        stream.write(b"synthetic unattended modification")
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=suspect)
    digest = store.submit(_ID, maker.package())
    with pytest.raises(trust.PluginTrustError, match="runtime_integrity"):
        store.verify_candidate(_ID, digest)


def test_failed_update_and_revocation_survive_reopen(package_maker, tmp_path):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    base = maker.package("1.0.0")
    replacement = maker.package("1.1.0")
    enabled = _enable(store, base)
    failed = store.submit(_ID, b"ordinary invalid fixture")
    with pytest.raises(trust.PluginTrustError, match="invalid_package"):
        store.verify_candidate(_ID, failed)
    assert store.enabled(_ID) == enabled
    candidate = _review(store, replacement)
    store.fail_update(_ID, candidate)
    assert store.status(_ID)["last_failure"] == "update_failed"
    reopened = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    assert reopened.enabled(_ID) == enabled
    reopened.revoke(_ID, enabled.version)
    assert reopened.status(_ID)["latest_revoked"]
    with pytest.raises(trust.PluginTrustError, match="not_enabled"):
        reopened.enabled(_ID)
    digest = reopened.submit(_ID, base)
    with pytest.raises(trust.PluginTrustError, match="version_revoked"):
        reopened.verify_candidate(_ID, digest)
    assert _enable(reopened, replacement).version == "1.1.0"
    digest = reopened.submit(_ID, base)
    with pytest.raises(trust.PluginTrustError, match="version_revoked"):
        reopened.verify_candidate(_ID, digest)
    assert reopened.enabled(_ID).version == "1.1.0"


def test_policy_revision_and_pending_approval_are_monotonic(package_maker, tmp_path):
    maker = package_maker
    original = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    _enable(original, maker.package())
    next_archive = maker.package("1.1.0")
    next_digest = _review(original, next_archive)
    updated = trust.PluginTrustStore(tmp_path, maker.policy(revision=2), node=maker.node)
    with pytest.raises(trust.PluginTrustError, match="stale_policy"):
        original.enabled(_ID)
    with pytest.raises(trust.PluginTrustError, match="not_enabled"):
        updated.enabled(_ID)
    with pytest.raises(trust.PluginTrustError, match="candidate_changed"):
        updated.enable(_ID, next_digest, authorized=True)
    with pytest.raises(trust.PluginTrustError, match="policy_rollback"):
        trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    _enable(updated, next_archive)
    revoked = trust.PluginTrustStore(tmp_path, maker.policy(revision=3, revoked=True), node=maker.node)
    assert revoked.status(_ID)["latest_revoked"]
    with pytest.raises(trust.PluginTrustError, match="policy_revocation_rollback"):
        trust.PluginTrustStore(tmp_path, maker.policy(revision=4), node=maker.node)


def test_stale_verification_cannot_overwrite_new_candidate_20_rounds(package_maker, tmp_path, monkeypatch):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    old, new = maker.package("1.0.0"), maker.package("1.1.0")
    original_verify = trust._verify_signature
    entered, resume = threading.Event(), threading.Event()

    def observe(*args):
        original_verify(*args)
        entered.set()
        if not resume.wait(5):
            raise RuntimeError("fixture barrier timed out")

    monkeypatch.setattr(trust, "_verify_signature", observe)
    with ThreadPoolExecutor(max_workers=1) as executor:
        for _ in range(20):
            entered.clear()
            resume.clear()
            digest = store.submit(_ID, old)
            pending = executor.submit(store.verify_candidate, _ID, digest)
            try:
                assert entered.wait(5)
                current = store.submit(_ID, new)
            finally:
                resume.set()
            with pytest.raises(trust.PluginTrustError, match="candidate_changed"):
                pending.result(timeout=6)
            status = store.status(_ID)
            assert status["candidate_sha"] == current
            assert status["candidate_state"] == "pending_validation"
            assert status["last_failure"] is None



def test_sqlite_activation_failure_does_not_replace_live_version(package_maker, tmp_path):
    import sqlite3

    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    active = _enable(store, maker.package())
    candidate = _review(store, maker.package("1.1.0"))
    database = tmp_path / "plugin-trust.sqlite3"
    with sqlite3.connect(database) as db:
        db.execute("""CREATE TRIGGER reject_fixture_activation BEFORE UPDATE OF active ON package
                      BEGIN SELECT RAISE(ABORT,'fixture activation aborted'); END""")
    db.close()
    with pytest.raises(sqlite3.IntegrityError, match="fixture activation aborted"):
        store.enable(_ID, candidate, authorized=True)
    assert store.enabled(_ID) == active
    assert store.status(_ID)["candidate_state"] == "reviewed"
    with sqlite3.connect(database) as db:
        db.execute("DROP TRIGGER reject_fixture_activation")
    db.close()
    assert store.enable(_ID, candidate, authorized=True).version == "1.1.0"


@pytest.mark.skipif(os.name != "nt", reason="Windows runtime share modes")
def test_runtime_file_is_pinned_during_actual_signature_verifier(package_maker, tmp_path, monkeypatch):
    maker = package_maker
    store = trust.PluginTrustStore(tmp_path, maker.policy(), node=maker.node)
    candidate = store.submit(_ID, maker.package())
    observed = []
    original = subprocess.run

    def observe(*args, **kwargs):
        with pytest.raises(PermissionError):
            with maker.node.open("r+b"):
                pass
        observed.append("image_write_denied")
        return original(*args, **kwargs)

    monkeypatch.setattr(trust.subprocess, "run", observe)
    assert store.verify_candidate(_ID, candidate).version == "1.0.0"
    assert observed == ["image_write_denied"]
