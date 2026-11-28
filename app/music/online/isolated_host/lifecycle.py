"""Ownership records for the explicitly enabled AppContainer host."""
from __future__ import annotations

import json
import os
import re
import shutil
import stat
from itertools import islice
from pathlib import Path

from app.core.logger import get_logger

from . import win32


logger = get_logger()
_OWNER = re.compile(r"(sl25-[0-9a-f]{32})\.json")
_PROFILE = re.compile(r"\.profile-(SL25[0-9a-f]{32})\.json")
_REPARSE = stat.FILE_ATTRIBUTE_REPARSE_POINT


def _identity(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_file_attributes & _REPARSE or not info.st_ino:
        raise ValueError("isolated_workspace_identity")
    return [info.st_dev, info.st_ino]


def _read(path):
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_file_attributes & _REPARSE
            or info.st_nlink != 1 or not 0 < info.st_size <= 1024):
        raise ValueError("isolated_workspace_record")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise ValueError("isolated_workspace_record_changed")
        return json.loads(stream.read(1025))


def _write(path, record):
    temporary = path.with_suffix(".pending")
    with temporary.open("x", encoding="ascii") as stream:
        json.dump(record, stream, ensure_ascii=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(path)


def _remove_profile(record, name):
    sid = win32.profile_sid(name)
    if _read(record) != {"name": name, "sid": sid}:
        raise ValueError("isolated_profile_identity")
    if win32.profile_exists(sid):
        if win32.delete_profile(name) != "0x00000000" or win32.profile_exists(sid):
            return False
    record.unlink()
    return True


class WorkspaceLifecycle:
    def __init__(self, parent, trust_root):
        self.parent = win32._owned_parent(parent)
        self.user = win32._user_sid()
        # Keep ownership outside the disposable tree. A second crash during
        # rmtree must not erase the evidence needed to finish cleanup.
        ledger = win32._owned_parent(trust_root) / ".starlight-lifecycle"
        ledger.mkdir(exist_ok=True)
        self.ledger = win32._owned_parent(ledger)
        self.recovered = []
        self.deferred = []
        candidates = ((entry, match[1]) for entry in self.ledger.iterdir()
                      if (match := _OWNER.fullmatch(entry.name)))
        for receipt, name in islice(candidates, 64):
            try:
                if self._reclaim(receipt, name):
                    self.recovered.append(name)
            except (OSError, ValueError, TypeError):
                self.deferred.append(name)
        if self.deferred:
            logger.warning("[Isolated music] %d workspace(s) could not be safely reclaimed", len(self.deferred))

        self.root = win32.create_workspace(self.parent)
        self.record = {
            "version": 1, "workspace": self.root.name, "user": self.user,
            "parent": str(self.parent), "identity": _identity(self.root), "pid": os.getpid(),
            "created": win32.current_process_creation(),
        }
        # No runtime, writable plugin directory or profile exists before this commit.
        self.receipt = self.ledger / (self.root.name + ".json")
        _write(self.receipt, self.record)

    def _ownership(self, receipt, name):
        if win32._owned_parent(self.ledger) != self.ledger or receipt.parent != self.ledger:
            raise ValueError("isolated_workspace_path")
        record = _read(receipt)
        if (not isinstance(record, dict)
                or set(record) != {"version", "workspace", "user", "parent", "identity", "pid", "created"}
                or record["version"] != 1 or record["workspace"] != name
                or record["user"] != self.user or record["parent"] != str(self.parent)
                or not isinstance(record["identity"], list) or len(record["identity"]) != 2
                or type(record["pid"]) is not int or not 0 < record["pid"] < 2**32
                or type(record["created"]) is not int or not 0 < record["created"] < 2**64):
            raise ValueError("isolated_workspace_record")
        return record

    def _reclaim(self, receipt, name):
        with win32.workspace_claim(name) as claimed:
            if not claimed:
                return False
            record = self._ownership(receipt, name)
            if not win32.owner_gone(record["pid"], record["created"]):
                return False
            if not self._remove(self.parent / name, receipt, record):
                raise OSError("isolated_workspace_incomplete")
            return True

    def _matches(self, root, record):
        return win32._owned_parent(root) == root and root.parent == self.parent and _identity(root) == record["identity"]

    def _remove(self, root, receipt, record):
        if self._ownership(receipt, root.name) != record:
            return False
        try:
            if not self._matches(root, record):
                return False
        except FileNotFoundError:
            # An interrupted cleanup may have removed the root before the receipt.
            if win32._owned_parent(self.parent) != self.parent:
                return False
            receipt.unlink()
            return True
        profiles = []
        for entry in root.iterdir():
            match = _PROFILE.fullmatch(entry.name)
            if match:
                name = match[1]
                if _read(entry) != {"name": name, "sid": win32.profile_sid(name)}:
                    return False
                profiles.append((entry, name))
        for entry, name in profiles:
            if not _remove_profile(entry, name):
                return False

        if not self._matches(root, record):
            return False
        # Since Python 3.8, rmtree on Windows removes directory junctions without
        # entering them. The root itself must still match the sealed identity.
        shutil.rmtree(root)
        receipt.unlink()
        return True

    def _profile_record(self, name):
        if not _PROFILE.fullmatch(f".profile-{name}.json"):
            raise ValueError("isolated_profile_name")
        return self.root / f".profile-{name}.json"

    def reserve_profile(self, name):
        sid = win32.profile_sid(name)
        if win32.profile_exists(sid):
            raise ValueError("isolated_profile_already_exists")
        _write(self._profile_record(name), {"name": name, "sid": sid})
        return sid

    def forget_profile(self, name):
        self._profile_record(name).unlink()

    def release_profile(self, name):
        return _remove_profile(self._profile_record(name), name)

    def close(self):
        try:
            with win32.workspace_claim(self.root.name) as claimed:
                return claimed and self._remove(self.root, self.receipt, self.record)
        except (OSError, ValueError, TypeError):
            return False
