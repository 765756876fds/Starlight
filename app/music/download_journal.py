"""Small SQLite journal for downloads, separate from the music library."""

import json
import logging
import os
import sqlite3
import stat
from contextlib import closing, contextmanager
from pathlib import Path

log = logging.getLogger(__name__)
MAX_RECORDS = 500
MAX_ROW_BYTES = 128 * 1024

_RAW_FIELDS = {
    "id", "songmid", "songId", "songIdStr", "musicId", "hash", "rid", "strMediaMid",
    "platform", "source", "name", "title", "artist", "singer", "album", "albumId", "albumMid",
    "duration", "interval", "isrc", "ISRC", "musicbrainzRecordingId", "mbRecordingId",
    "recording_mbid", "version", "versionName", "subtitle", "versionType", "mixName",
}
_QUALITY_FIELDS = {"size", "sizeBytes", "fileSize", "format", "bitrate", "sampleRate", "bitDepth", "quality"}
_QUALITY_NAMES = {"flac24bit", "flac", "320k", "128k", "super", "high", "standard", "low", "lossless", "hires"}


def plain(value, limit=512):
    if (isinstance(value, str) and len(value) <= limit and "://" not in value
            and not value.startswith(("//", "\\")) and not any(ord(c) < 32 for c in value)):
        return value
    return ""


def _raw(data, nested=False):
    if not isinstance(data, dict):
        return {}
    selected = {}
    for name in _RAW_FIELDS:
        value = data.get(name)
        if type(value) is int or (type(value) is float and -1e9 < value < 1e9):
            selected[name] = value
        elif text := plain(value):
            selected[name] = text
    if isinstance(data.get("qualities"), dict):
        selected["qualities"] = {
            key: {name: value for name, value in item.items() if name in _QUALITY_FIELDS
                  and (type(value) is int or (type(value) is float and -1e12 < value < 1e12)
                       or plain(value, 80))}
            for key, item in data["qualities"].items() if key in _QUALITY_NAMES and isinstance(item, dict)
        }
    if not nested and isinstance(data.get("musicInfo"), dict):
        selected["musicInfo"] = _raw(data["musicInfo"], nested=True)
    return selected


def _track_data(data):
    fields = {key: plain(data.get(key)) for key in ("source_name", "title", "artist", "album")}
    track_id = data.get("track_id")
    fields["track_id"] = plain(str(track_id)) if type(track_id) in (str, int) else ""
    duration = data.get("duration_seconds") or data.get("duration") or 0
    fields["duration_seconds"] = duration if type(duration) in (int, float) and 0 < duration < 86400 else 0
    extra = data.get("extra")
    raw = _raw(extra.get("_raw", extra.get("raw"))) if isinstance(extra, dict) else {}
    fields["extra"] = {"_raw": raw} if raw else {}
    authority = data.get("authorization_plugin")
    if authority is not None:
        if plain(authority, 128) != authority or not authority or not fields["source_name"] or not fields["track_id"]:
            raise ValueError("Invalid stored authorization identity")
        fields["authorization_plugin"] = authority
    return fields


def song_record(song):
    result = _track_data(song)
    if "_download_alternatives" in song:
        result["alternatives"] = [_track_data(vars(track)) for track in song["_download_alternatives"][:36]]
    return result


def restore_song(data):
    from .online.models import OnlineTrack

    if not isinstance(data, dict):
        raise ValueError("Invalid song record")
    result = _track_data(data)
    if not result["title"] or bool(result["source_name"]) != bool(result["track_id"]):
        raise ValueError("Invalid stored recording identity")
    if "alternatives" in data:
        result["_download_alternatives"] = tuple(
            OnlineTrack(**_track_data(row)) for row in data["alternatives"][:36]
            if isinstance(row, dict) and plain(row.get("source_name")) and plain(row.get("track_id"))
        )
    return result


def is_link(info):
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def checked_directory(path):
    """Reject symlinks and Windows reparse points in all directory components."""
    path = Path(os.path.abspath(path))
    for ancestor in reversed((path, *path.parents)):
        info = ancestor.lstat()
        if is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise OSError("Unsafe download directory")
    return path


@contextmanager
def pin_directory(path):
    """On Windows, prevent replacement of the destination and its ancestors."""
    directory = checked_directory(path)
    if os.name != "nt":
        yield directory
        return

    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    open_directory = kernel.CreateFileW
    open_directory.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                               wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    open_directory.restype = wintypes.HANDLE
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handles = []
    try:
        for component in reversed((directory, *directory.parents)):
            if component.parent == component:
                continue
            before = component.lstat()
            # Read attributes, share reads/writes, deny delete/rename. Open the
            # component itself if it has become a reparse point since lstat.
            handle = open_directory(str(component), 0x80, 3, None, 3, 0x02200000, None)
            if handle == ctypes.c_void_p(-1).value:
                raise ctypes.WinError(ctypes.get_last_error())
            handles.append(handle)
            after = component.lstat()
            if is_link(after) or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
                raise OSError("Download directory changed")
        yield checked_directory(directory)
    finally:
        for handle in reversed(handles):
            close(handle)


class DownloadJournal:
    """One writer per application profile, SQLite commits per affected task."""

    def __init__(self, path):
        self.path = Path(os.path.abspath(path))
        self.lease = None
        self.available = False
        try:
            directory = checked_directory(self.path.parent)
            lease_path = directory / (self.path.name + ".lock")
            if lease_path.exists() or lease_path.is_symlink():
                info = lease_path.lstat()
                if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise OSError("Unsafe journal lock")
            self.lease = lease_path.open("a+b")
            self.lease.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.lease.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
            for suffix in ("", "-journal", "-shm", "-wal"):
                candidate = self.path.with_name(self.path.name + suffix)
                if os.path.lexists(candidate):
                    info = candidate.lstat()
                    if is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise OSError("Unsafe download journal")
            with closing(self._connect()) as db, db:
                version = db.execute("PRAGMA user_version").fetchone()[0]
                if version > 1:
                    raise sqlite3.DatabaseError("unsupported journal schema")
                db.execute("CREATE TABLE IF NOT EXISTS downloads (key TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                db.execute("PRAGMA user_version=1")
            self.available = True
        except (OSError, sqlite3.Error) as exc:
            log.warning("download journal unavailable type=%s", type(exc).__name__)
            self.close()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=.15)

    def rows(self):
        if not self.available:
            return []
        rows = []
        try:
            with closing(self._connect()) as db:
                for key, payload in db.execute(
                    "SELECT key, payload FROM downloads WHERE length(payload) <= ? ORDER BY rowid LIMIT ?",
                    (MAX_ROW_BYTES, MAX_RECORDS),
                ):
                    try:
                        data = json.loads(payload)
                    except (TypeError, ValueError, RecursionError):
                        continue
                    if isinstance(data, dict):
                        rows.append((key, data))
        except sqlite3.Error as exc:
            self.available = False
            log.warning("download journal read failed type=%s", type(exc).__name__)
        return rows

    def save(self, key, record):
        if not self.available:
            raise OSError("Download journal unavailable")
        payload = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(payload.encode()) > MAX_ROW_BYTES:
            raise OSError("Download task record too large")
        try:
            with closing(self._connect()) as db, db:
                db.execute(
                    "INSERT INTO downloads VALUES(?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                    (key, payload),
                )
        except sqlite3.Error as exc:
            raise OSError("Download journal write failed") from exc

    def delete(self, key):
        if not self.available:
            raise OSError("Download journal unavailable")
        try:
            with closing(self._connect()) as db, db:
                db.execute("DELETE FROM downloads WHERE key=?", (key,))
        except sqlite3.Error as exc:
            raise OSError("Download journal removal failed") from exc

    def close(self):
        self.available = False
        if self.lease is not None:
            self.lease.close()
            self.lease = None
