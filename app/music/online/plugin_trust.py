
"""Offline package trust for a future isolated plugin runner.

The caller pins owner policy and a trusted host Node executable. Verified
packages are data only. The runner must separately restrict module resolution,
grant plugin-read-only access (or consume the verified bytes directly), pin the
runtime's installed dependencies, and enforce policy at execution time.
"""
from __future__ import annotations

from base64 import b64decode, b64encode
import binascii
from contextlib import closing, contextmanager
from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import subprocess
from types import MappingProxyType
from typing import Mapping
import zlib
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile

MAX_PACKAGE = 8 * 1024 * 1024
MAX_FILE = 1024 * 1024
MAX_FILES = 64
MAX_DOCUMENT = 64 * 1024
_MANIFEST = "manifest.json"
_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,39}", re.ASCII)
_PART = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", re.ASCII)
_RESERVED = re.compile(r"CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9]", re.IGNORECASE | re.ASCII)
_DIGEST = re.compile(r"[0-9a-f]{64}", re.ASCII)
_VERSION = re.compile(r"(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")
_ED25519_SPKI = bytes.fromhex("302a300506032b6570032100")

# Same Node crypto primitive as the B23 prototype. No package code is launched.
_VERIFY = """
const fs = require('node:fs');
const { createPublicKey, verify } = require('node:crypto');
try {
  const q = JSON.parse(fs.readFileSync(0, 'utf8'));
  const key = createPublicKey({ key: Buffer.from(q.key, 'base64'), format: 'der', type: 'spki' });
  if (key.asymmetricKeyType !== 'ed25519' ||
      !verify(null, Buffer.from(q.payload, 'base64'), key, Buffer.from(q.signature, 'base64'))) process.exitCode = 1;
} catch { process.exitCode = 1; }
"""


class PluginTrustError(ValueError):
    """Stable error code suitable for an operator-facing status."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _digest(value):
    return isinstance(value, str) and _DIGEST.fullmatch(value) is not None


def _id(value):
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _version(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match:
        raise PluginTrustError("invalid_version")
    return tuple(map(int, match.groups()))


def _canonical(data):
    return json.dumps(data, ensure_ascii=True, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("ascii")


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PluginTrustError("duplicate_field")
        result[key] = value
    return result


def _document(raw, fields):
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
        raise PluginTrustError("invalid_document") from error
    if not isinstance(data, dict) or data.keys() != fields:
        raise PluginTrustError("unexpected_fields")
    return data


def _b64(value, length):
    if not isinstance(value, str) or len(value) != 4 * ((length + 2) // 3):
        raise PluginTrustError("invalid_signature_encoding")
    try:
        decoded = b64decode(value, validate=True)
    except (ValueError, binascii.Error) as error:
        raise PluginTrustError("invalid_signature_encoding") from error
    if len(decoded) != length or b64encode(decoded).decode("ascii") != value:
        raise PluginTrustError("invalid_signature_encoding")
    return decoded


def _package_path(name):
    if not isinstance(name, str) or not 0 < len(name) <= 240:
        return False
    return all(_PART.fullmatch(part) and not part.endswith(".")
               and not _RESERVED.fullmatch(part.split(".", 1)[0])
               for part in name.split("/"))


def _plain_file(path):
    info = path.lstat()
    reparse = getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    if not stat.S_ISREG(info.st_mode) or reparse or info.st_nlink != 1:
        raise PluginTrustError("unsafe_host_path")
    return info


def _owned_directory(path):
    path = Path(os.path.abspath(path))
    for directory in reversed((path, *path.parents)):
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or (
                getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise PluginTrustError("unsafe_host_path")
    return path


@dataclass(frozen=True)
class PublisherGrant:
    publisher: str
    minimum: tuple[int, int, int]
    runtime_sha256: str


@dataclass(frozen=True)
class TrustPolicy:
    revision: int
    sha256: str
    publishers: Mapping[str, str]
    plugins: Mapping[str, PublisherGrant]
    revoked_publishers: frozenset[str]

    @classmethod
    def from_owner_document(cls, raw: bytes, *, pinned_sha256: str) -> TrustPolicy:
        if (type(raw) is not bytes or len(raw) > MAX_DOCUMENT or not _digest(pinned_sha256)
                or _sha(raw) != pinned_sha256):
            raise PluginTrustError("policy_pin_mismatch")
        data = _document(raw, {"revision", "publishers", "plugins", "revoked_publishers"})
        revision, publishers, plugins = data["revision"], data["publishers"], data["plugins"]
        if (type(revision) is not int or not 1 <= revision <= (1 << 53) - 1
                or not isinstance(publishers, dict) or not isinstance(plugins, dict)):
            raise PluginTrustError("invalid_policy")
        revoked = data["revoked_publishers"]
        if not isinstance(revoked, list) or not all(_id(name) for name in revoked):
            raise PluginTrustError("invalid_policy")
        for name, key in publishers.items():
            if not _id(name) or not _b64(key, 44).startswith(_ED25519_SPKI):
                raise PluginTrustError("invalid_publisher_key")
        grants = {}
        for name, grant in plugins.items():
            if (not _id(name) or not isinstance(grant, dict)
                    or grant.keys() != {"publisher", "minimum_version", "runtime_sha256"}
                    or not _id(grant["publisher"]) or grant["publisher"] not in publishers
                    or not _digest(grant["runtime_sha256"])):
                raise PluginTrustError("invalid_grant")
            grants[name] = PublisherGrant(grant["publisher"], _version(grant["minimum_version"]),
                                          grant["runtime_sha256"])
        return cls(revision, pinned_sha256, MappingProxyType(dict(publishers)),
                   MappingProxyType(grants), frozenset(revoked))


@dataclass(frozen=True)
class VerifiedPlugin:
    """The exact bytes reviewed in one immutable in-memory holding object."""

    plugin_id: str
    publisher: str
    version: str
    entry: str
    runtime_sha256: str
    package_sha256: str
    policy_sha256: str
    files: Mapping[str, bytes]


def _archive_files(raw: bytes) -> dict[str, bytes]:
    if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKAGE:
        raise PluginTrustError("package_size")
    try:
        with ZipFile(BytesIO(raw)) as package:
            entries = package.infolist()
            if not 2 <= len(entries) <= MAX_FILES + 1:
                raise PluginTrustError("file_count")
            seen = set()
            total = 0
            for info in entries:
                path = info.orig_filename
                key = path.casefold()
                kind = stat.S_IFMT(info.external_attr >> 16)
                size_limit = MAX_DOCUMENT if path == _MANIFEST else MAX_FILE
                total += info.file_size
                if (not _package_path(path) or key in seen or kind not in (0, stat.S_IFREG)
                        or info.external_attr & (stat.FILE_ATTRIBUTE_REPARSE_POINT | stat.FILE_ATTRIBUTE_DIRECTORY)
                        or info.flag_bits & 1 or info.compress_type not in (ZIP_STORED, ZIP_DEFLATED)
                        or info.file_size > size_limit or total > MAX_PACKAGE):
                    raise PluginTrustError("invalid_package_entry")
                seen.add(key)
            # File and directory names must also be unambiguous on Windows.
            spellings = {}
            for info in entries:
                parts = info.filename.split("/")
                for n in range(1, len(parts) + 1):
                    prefix = "/".join(parts[:n])
                    folded = prefix.casefold()
                    if (spellings.setdefault(folded, prefix) != prefix
                            or (n < len(parts) and folded in seen)):
                        raise PluginTrustError("invalid_package_entry")
            return {info.filename: package.read(info) for info in entries}
    except (BadZipFile, RuntimeError, NotImplementedError, OSError, EOFError, zlib.error) as error:
        raise PluginTrustError("invalid_package") from error


@contextmanager
def _pinned_runtime(path: Path):
    if os.name != "nt":
        with path.open("rb") as stream:
            yield stream
        return
    # Keep the Windows image open without write/delete sharing through launch.
    # Host ACLs still protect the installed directory tree and runtime DLLs.
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_file = kernel.CreateFileW
    open_file.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                          wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    open_file.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    open_reparse_point = 0x00200000
    handle = open_file(str(path), generic_read, file_share_read, None, open_existing, open_reparse_point, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except OSError:
        kernel.CloseHandle(handle)
        raise
    with os.fdopen(descriptor, "rb") as stream:
        yield stream


def _verify_signature(node: Path, runtime_sha256: str, public_key: str,
                      signature: str, payload: bytes) -> None:
    _b64(signature, 64)
    request = _canonical({"key": public_key, "signature": signature,
                          "payload": b64encode(payload).decode("ascii")})
    env = {name: os.environ[name] for name in ("SystemRoot", "WINDIR") if name in os.environ}
    try:
        node = _owned_directory(node.parent) / node.name
        initial_stat = _plain_file(node)
        with _pinned_runtime(node) as stream:
            runtime_stat = os.fstat(stream.fileno())
            if (not os.path.samestat(initial_stat, runtime_stat)
                    or getattr(runtime_stat, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
                    or hashlib.file_digest(stream, "sha256").hexdigest() != runtime_sha256
                    or not os.path.samestat(runtime_stat, _plain_file(node))):
                raise PluginTrustError("runtime_integrity")
            result = subprocess.run([str(node), "-e", _VERIFY], cwd=node.parent,
                                    input=request, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    env=env, timeout=5, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired) as error:
        raise PluginTrustError("verifier_unavailable") from error
    if result.returncode:
        raise PluginTrustError("signature_invalid")


def _verify(raw: bytes, plugin_id: str, policy: TrustPolicy, node: Path) -> VerifiedPlugin:
    grant = policy.plugins.get(plugin_id)
    if not grant or grant.publisher in policy.revoked_publishers:
        raise PluginTrustError("publisher_not_authorized")
    files = _archive_files(raw)
    manifest_bytes = files.pop(_MANIFEST, None)
    if manifest_bytes is None:
        raise PluginTrustError("manifest_missing")
    manifest = _document(manifest_bytes, {"id", "publisher", "version", "entry",
                                          "runtime_sha256", "files", "signature"})
    entry, version = manifest["entry"], manifest["version"]
    if (manifest["id"] != plugin_id or manifest["publisher"] != grant.publisher
            or manifest["runtime_sha256"] != grant.runtime_sha256):
        raise PluginTrustError("identity_mismatch")
    if _version(version) < grant.minimum:
        raise PluginTrustError("version_rollback")
    declared = manifest["files"]
    if (not isinstance(declared, dict) or declared.keys() != files.keys()
            or not isinstance(entry, str) or entry not in files or not entry.endswith(".js")):
        raise PluginTrustError("file_set_mismatch")
    signature = manifest.pop("signature")
    _verify_signature(node, grant.runtime_sha256, policy.publishers[grant.publisher],
                      signature, _canonical(manifest))
    for name, contents in files.items():
        if not _digest(declared[name]) or _sha(contents) != declared[name]:
            raise PluginTrustError("file_integrity")
    return VerifiedPlugin(plugin_id, grant.publisher, version, entry, grant.runtime_sha256,
                          _sha(raw), policy.sha256, MappingProxyType(files))


class PluginTrustStore:
    """Owner-only staging and approval in a transactional SQLite store.

    Submit uses a ZIP of exact manifest paths and their bytes. The manifest's
    sorted, compact UTF-8 JSON without its signature is signed with Ed25519.
    Each file uses SHA-256, and the policy binds the publisher and runtime pin.

    The supplied root must have a host-owned ACL and remain inaccessible to
    plugins. Nothing reads this store from the default bridge.
    """

    def __init__(self, root: Path, policy: TrustPolicy, *, node: Path):
        self._root = _owned_directory(root)
        self._database = self._root / "plugin-trust.sqlite3"
        self._node = Path(node)
        self._policy = policy
        with self._transaction(check_policy=False) as db:
            db.execute("CREATE TABLE IF NOT EXISTS policy (revision INTEGER, digest TEXT)")
            db.execute("""CREATE TABLE IF NOT EXISTS package (
                         id TEXT PRIMARY KEY, candidate BLOB, candidate_sha TEXT,
                         candidate_state TEXT, candidate_policy TEXT, active BLOB, active_sha TEXT, active_policy TEXT,
                         latest_version TEXT, latest_sha TEXT, last_failure TEXT)""")
            db.execute("""CREATE TABLE IF NOT EXISTS revoked (
                         id TEXT, version TEXT, PRIMARY KEY (id, version))""")
            db.execute("CREATE TABLE IF NOT EXISTS revoked_publisher (id TEXT PRIMARY KEY)")
            known_revocations = {row[0] for row in db.execute("SELECT id FROM revoked_publisher")}
            if not known_revocations <= policy.revoked_publishers:
                raise PluginTrustError("policy_revocation_rollback")
            stored = db.execute("SELECT revision, digest FROM policy").fetchone()
            if stored and (policy.revision < stored["revision"] or
                           (policy.revision == stored["revision"] and policy.sha256 != stored["digest"])):
                raise PluginTrustError("policy_rollback")
            if stored is None or policy.revision > stored["revision"]:
                db.executemany("INSERT OR IGNORE INTO revoked_publisher VALUES (?)",
                               ((publisher,) for publisher in policy.revoked_publishers))
                db.execute("DELETE FROM policy")
                db.execute("INSERT INTO policy VALUES (?, ?)", (policy.revision, policy.sha256))
                # Permanent deny for currently enabled versions invalidated by a new owner policy.
                for row in db.execute("SELECT id, latest_version FROM package WHERE active IS NOT NULL").fetchall():
                    grant = policy.plugins.get(row["id"])
                    if (not grant or grant.publisher in policy.revoked_publishers
                            or _version(row["latest_version"]) < grant.minimum):
                        self._revoke(db, row["id"], row["latest_version"])

    @contextmanager
    def _transaction(self, *, check_policy=True):
        _owned_directory(self._root)
        for path in (self._database, self._database.with_suffix(".sqlite3-journal"),
                     self._database.with_suffix(".sqlite3-wal"), self._database.with_suffix(".sqlite3-shm")):
            if os.path.lexists(path):
                _plain_file(path)
        with closing(sqlite3.connect(self._database, timeout=5)) as db, db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            if check_policy:
                current = db.execute("SELECT revision, digest FROM policy").fetchone()
                if tuple(current) != (self._policy.revision, self._policy.sha256):
                    raise PluginTrustError("stale_policy")
            yield db

    def _check_choice(self, db, verified, row):
        revoked = db.execute("SELECT 1 FROM revoked WHERE id=? AND version=?",
                             (verified.plugin_id, verified.version)).fetchone()
        if revoked:
            raise PluginTrustError("version_revoked")
        if row["latest_version"]:
            latest, count = row["latest_version"], _version(row["latest_version"])
            if (_version(verified.version) < count
                    or (verified.version == latest and verified.package_sha256 != row["latest_sha"])):
                raise PluginTrustError("version_rollback")

    def _revoke(self, db, plugin_id, version):
        db.execute("INSERT OR IGNORE INTO revoked VALUES (?, ?)", (plugin_id, version))
        db.execute("UPDATE package SET active=NULL, active_sha=NULL, active_policy=NULL "
                   "WHERE id=? AND latest_version=?", (plugin_id, version))

    def submit(self, plugin_id: str, raw: bytes) -> str:
        if not _id(plugin_id) or plugin_id not in self._policy.plugins:
            raise PluginTrustError("plugin_not_authorized")
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_PACKAGE:
            raise PluginTrustError("package_size")
        digest = _sha(raw)
        with self._transaction() as db:
            db.execute("INSERT OR IGNORE INTO package(id) VALUES (?)", (plugin_id,))
            db.execute("""UPDATE package SET candidate=?, candidate_sha=?, candidate_state='pending_validation',
                       candidate_policy=?, last_failure=NULL WHERE id=?""", (raw, digest, self._policy.sha256, plugin_id))
        return digest

    def verify_candidate(self, plugin_id: str, digest: str) -> VerifiedPlugin:
        with self._transaction() as db:
            row = self._candidate(db, plugin_id, digest, "pending_validation")
            raw = row["candidate"]
        try:
            verified = _verify(raw, plugin_id, self._policy, self._node)
            with self._transaction() as db:
                row = self._candidate(db, plugin_id, digest, "pending_validation")
                if verified.package_sha256 != digest:
                    raise PluginTrustError("candidate_integrity")
                self._check_choice(db, verified, row)
                db.execute("UPDATE package SET candidate_state='pending_review' WHERE id=?", (plugin_id,))
        except (PluginTrustError, OSError) as error:
            code = error.code if isinstance(error, PluginTrustError) else "verifier_unavailable"
            try:
                self.fail_update(plugin_id, digest, integrity_failure=True)
            except PluginTrustError:
                pass  # A newer candidate or policy retains its independent state.
            raise PluginTrustError(code) from error
        return verified

    def _candidate(self, db, plugin_id, digest, state):
        row = db.execute("SELECT * FROM package WHERE id=?", (plugin_id,)).fetchone()
        if (not row or row["candidate_sha"] != digest or row["candidate_state"] != state
                or row["candidate_policy"] != self._policy.sha256):
            raise PluginTrustError("candidate_changed")
        return row

    def review(self, plugin_id: str, digest: str) -> None:
        with self._transaction() as db:
            self._candidate(db, plugin_id, digest, "pending_review")
            db.execute("UPDATE package SET candidate_state='reviewed' WHERE id=?", (plugin_id,))

    def enable(self, plugin_id: str, digest: str, *, authorized: bool) -> VerifiedPlugin:
        if authorized is not True:
            raise PluginTrustError("authorization_pending")
        with self._transaction() as db:
            raw = self._candidate(db, plugin_id, digest, "reviewed")["candidate"]
        verified = _verify(raw, plugin_id, self._policy, self._node)
        with self._transaction() as db:
            row = self._candidate(db, plugin_id, digest, "reviewed")
            if verified.package_sha256 != digest:
                raise PluginTrustError("candidate_integrity")
            self._check_choice(db, verified, row)
            db.execute("""UPDATE package SET active=?, active_sha=?, active_policy=?, latest_version=?, latest_sha=?,
                       candidate=NULL, candidate_sha=NULL, candidate_state=NULL, candidate_policy=NULL,
                       last_failure=NULL WHERE id=?""",
                       (raw, digest, self._policy.sha256, verified.version, digest, plugin_id))
        return verified

    def fail_update(self, plugin_id: str, digest: str, *, integrity_failure=False) -> None:
        state = "integrity_failed" if integrity_failure else "update_failed"
        with self._transaction() as db:
            changed = db.execute("""UPDATE package SET candidate_state=?, last_failure=?, candidate=NULL
                                 WHERE id=? AND candidate_sha=? AND candidate_policy=? AND candidate_state IN
                                 ('pending_validation','pending_review','reviewed')""",
                                 (state, state, plugin_id, digest, self._policy.sha256))
            if not changed.rowcount:
                raise PluginTrustError("candidate_changed")

    def revoke(self, plugin_id: str, version: str) -> None:
        _version(version)
        if not _id(plugin_id):
            raise PluginTrustError("invalid_plugin")
        with self._transaction() as db:
            self._revoke(db, plugin_id, version)

    def status(self, plugin_id: str) -> dict | None:
        with self._transaction() as db:
            row = db.execute("""SELECT candidate_sha, candidate_state, candidate_policy, active_sha, active_policy, latest_version,
                             last_failure,
                             EXISTS (SELECT 1 FROM revoked WHERE id=package.id AND version=package.latest_version)
                             AS latest_revoked FROM package WHERE id=?""", (plugin_id,)).fetchone()
            if not row:
                return None
            value = dict(row)
            value["authorization_pending"] = (value["candidate_state"] == "reviewed"
                                              or (value["candidate_sha"] is not None
                                                  and value["candidate_policy"] != self._policy.sha256)
                                              or (value["active_sha"] is not None
                                                  and value["active_policy"] != self._policy.sha256))
            return value

    def enabled(self, plugin_id: str) -> VerifiedPlugin:
        with self._transaction() as db:
            row = db.execute("SELECT * FROM package WHERE id=?", (plugin_id,)).fetchone()
            if row is None or row["active"] is None or row["active_policy"] != self._policy.sha256:
                raise PluginTrustError("not_enabled")
            raw, digest = row["active"], row["active_sha"]
        verified = _verify(raw, plugin_id, self._policy, self._node)
        with self._transaction() as db:
            row = db.execute("SELECT * FROM package WHERE id=?", (plugin_id,)).fetchone()
            if (verified.package_sha256 != digest or row["active_sha"] != digest
                    or row["active_policy"] != self._policy.sha256):
                raise PluginTrustError("active_changed")
            self._check_choice(db, verified, row)
        return verified
