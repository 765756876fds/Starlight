"""Bounded background downloads with conservative recording identity and media checks.

Try available quality tiers, compare inspected media, attach matching assets, then
publish without replacing existing user files. No player instance is involved.
"""

import errno
import os
import logging
import uuid
import threading
from copy import deepcopy
from dataclasses import replace
import time
import hashlib
import json
import re
import stat
from pathlib import Path
from dataclasses import asdict, dataclass, field

from .download_journal import DownloadJournal, MAX_RECORDS, checked_directory, is_link, pin_directory, plain, restore_song, song_record

_STOPPED = {"paused", "failed", "cancelled", "completed"}
_PART = re.compile(r"starlight-[0-9a-f]{32}\.part")
_DEVICE = re.compile(r"^(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])(?:\.|$)", re.I)


@dataclass
class _Attempt:
    generation: int
    callbacks: dict
    event: threading.Event = field(default_factory=threading.Event)
    future: object = None
    action: str = ""
    started: bool = False
    published: bool = False

from concurrent.futures import ThreadPoolExecutor
from .download_selection import CONTAINERS, LOSSLESS_CODECS, TIER_ORDER, DownloadOffer, DownloadOption, codec_family

_EXT_HINTS = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape")
log = logging.getLogger(__name__)
DEFAULT_DOWNLOAD_OPTIONS = dict(quality="best", lyrics=True, cover=True, metadata=True, allow_fallback=True)
PROBE_SECONDS = 8.0
PROBE_BYTES = 64 * 1024
MAX_RESOLUTIONS = 36
MAX_PROBES = 12
MAX_TRANSFERS = 6
_REFRESH_HTTP = {401, 403, 404, 410}
QUALITY_ORDER = ("super", "high", "standard", "low")


def normalize_options(options=None):
    result = dict(DEFAULT_DOWNLOAD_OPTIONS)
    if isinstance(options, dict):
        if options.get("quality") in ("best", *TIER_ORDER):
            result["quality"] = options["quality"]
        if options.get("format") in _EXT_HINTS:
            result["format"] = options["format"]
        if plain(options.get("verified_quality"), 96):
            result["verified_quality"] = options["verified_quality"]
        for key in ("lyrics", "cover", "metadata", "allow_fallback"):
            if type(options.get(key)) is bool:
                result[key] = options[key]
    return result


_ILLEGAL = '\\/:*?"<>|'


def freeze_download_song(song):
    from .online.models import OnlineTrack

    frozen = dict(song, extra=deepcopy(song.get("extra") or {}))
    if "_download_alternatives" in song:
        frozen["_download_alternatives"] = tuple(
            replace(track, extra=deepcopy(track.extra))
            for track in song["_download_alternatives"] if isinstance(track, OnlineTrack))
    return frozen


class DownloadManager:
    """并发受限的在线歌下载器。线程安全。"""

    def __init__(self, online_manager, db, download_dir, max_workers=3, *, journal_path=None):
        self.online_manager = online_manager
        self.db = db
        self.download_dir = os.path.abspath(download_dir)
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="music-dl")
        self._probe_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-dl-probe")
        self._probe_slots = threading.BoundedSemaphore(2)
        self._probe_futures = set()
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._active = 0
        self._verified = {}
        self._tasks = {}
        self._runs = {}
        self._current = threading.local()
        self._journal = DownloadJournal(journal_path) if journal_path is not None else None
        if self._journal:
            self._restore()

    @staticmethod
    def _key(title, artist):
        return f"{title or ''}|{artist or ''}".strip("|")

    @staticmethod
    def task_key(song, options=None):
        from .online.content_choices import raw_track
        source, native_id = song.get("source_name"), song.get("track_id")
        if source and native_id:
            identity = (source, str(native_id))
        else:
            track = DownloadManager._preferred_track(song)
            extra = raw_track(track) if track else (song.get("extra") or {}).get("_raw", {})
            extra = extra if isinstance(extra, dict) else {}
            duration = song.get("duration_seconds") or song.get("duration")
            identity = ("metadata", *(song.get(key) or "" for key in ("title", "artist", "album")),
                        float(duration) if type(duration) in (int, float) and 0 < duration < 86400 else 0.0,
                        *(extra.get(key) for key in ("ISRC", "isrc", "version", "versionName")))
        settings = normalize_options(options)
        choice = options.get("representation") if isinstance(options, dict) else None
        if isinstance(choice, DownloadOption):
            identity += (choice.resource, choice.extension, choice.codec, choice.bit_depth, choice.sample_rate)
        identity += (settings["quality"], settings.get("format"), settings.get("verified_quality"))
        return hashlib.sha256(json.dumps(identity, ensure_ascii=False, default=str).encode()).hexdigest()

    @staticmethod
    def _sanitize(name: str) -> str:
        safe = "".join("_" if ch in _ILLEGAL or ord(ch) < 32 else ch for ch in str(name))
        safe = safe.strip().rstrip(". ")
        if _DEVICE.match(safe):
            safe = "_" + safe
        return safe

    def _filename_base(self, title, artist) -> str:
        t = self._sanitize(title or "未知") or "未知"
        a = self._sanitize(artist or "")
        combined = f"{t} - {a}" if a else t
        # Leave room for the extension and a duplicate-name suffix on NTFS.
        return combined.encode("utf-16-le", errors="surrogatepass")[:300].decode("utf-16-le", errors="ignore").rstrip(". ")

    def _directory(self, *, create=False):
        path = Path(self.download_dir)
        if create and not path.exists():
            missing = []
            for parent in (path, *path.parents):
                if os.path.lexists(parent):
                    checked_directory(parent)
                    break
                missing.append(parent)
            for entry in reversed(missing):
                entry.mkdir(exist_ok=True)
        return checked_directory(path)

    def is_downloaded(self, title, artist) -> bool:
        return bool(self.find_download_path(title, artist))

    def find_download_path(self, title, artist) -> str:
        base = self._filename_base(title, artist)
        try:
            directory = self._directory()
            for ext in _EXT_HINTS:
                p = directory / (base + ext)
                if os.path.lexists(p):
                    info = p.lstat()
                    if stat.S_ISREG(info.st_mode) and not is_link(info):
                        return str(p)
        except OSError:
            pass
        return ""

    def tasks(self):
        with self._lock:
            return [self._snapshot(task) for task in self._tasks.values()]

    def get_task(self, key):
        with self._lock:
            return self._snapshot(self._tasks[key]) if key in self._tasks else None

    @staticmethod
    def _snapshot(task):
        out = {key: value for key, value in task.items() if key in (
            "key", "generation", "state", "message", "path", "bytes_written",
            "total_bytes", "speed_bps", "eta_seconds", "details")}
        request = task["request"]
        out["song"] = {k: request.get(k) for k in ("source_name", "track_id", "title", "artist", "album")}
        out["options"] = dict(request["_download_options"])
        out["details"] = dict(out["details"])
        return out

    def _persist(self, task):
        if self._journal is None:
            return
        request = task["request"]
        choice = request["_download_choice"]
        self._journal.save(task["key"], {
            "schema": 1, "directory": self.download_dir, "song": song_record(request),
            "options": request["_download_options"], "choice": asdict(choice) if choice else None,
            "generation": task["generation"], "state": task["state"], "message": task["message"],
            "final": os.path.basename(task["path"]), "partial": task.get("partial"),
            "pending": task.get("pending"), "stop": task.get("stop"),
            "prepared": task.get("prepared", []), "details": self._persisted_details(task["details"]),
            "bytes_written": task["bytes_written"], "total_bytes": task["total_bytes"],
        })

    @staticmethod
    def _persisted_details(details):
        keys = ("quality", "format", "codec", "size_bytes", "bitrate", "sample_rate",
                "bit_depth", "channels", "media_evidence", "fallback", "verified",
                "indexed", "lyrics", "cover", "metadata", "declared_quality")
        return {k: details[k] for k in keys if k in details and
                (type(details[k]) in (int, bool) or plain(details[k], 96))}

    @staticmethod
    def _empty_task(key, request):
        return dict(key=key, request=request, state="queued", generation=0, message="", path="",
                    bytes_written=0, total_bytes=None, speed_bps=0.0, eta_seconds=None, details={})

    @staticmethod
    def _safe_final(name):
        return (isinstance(name, str) and 0 < len(name) <= 180
                and name == DownloadManager._sanitize(name)
                and Path(name).suffix.lower() in _EXT_HINTS)

    @staticmethod
    def _stored_choice(data):
        if data is None:
            return None
        result = DownloadOption(**data)
        if (not plain(result.owner) or not plain(result.track_id)
                or result.extension not in _EXT_HINTS
                or result.tier not in (None, *TIER_ORDER)
                or (result.native is not None and not plain(result.native, 32))
                or any(value is not None and (type(value) is not int or value <= 0) for value in (
                    result.size_bytes, result.bitrate, result.sample_rate, result.bit_depth, result.channels))
                or any(value is not None and not plain(value, 96) for value in (
                    result.codec, result.size_evidence, result.declared_quality))):
            raise ValueError("Invalid stored rendition")
        return result

    def _restore(self):
        for key, row in self._journal.rows():
            try:
                if row.get("schema") != 1:
                    continue
                request = restore_song(row["song"])
                request["_download_options"] = normalize_options(row.get("options"))
                request["_download_choice"] = self._stored_choice(row.get("choice"))
                if self.task_key(request, dict(request["_download_options"],
                                               representation=request["_download_choice"])) != key:
                    continue
                task = self._empty_task(key, request)
                same_directory = row.get("directory") == self.download_dir
                generation = row.get("generation")
                task["generation"] = generation if type(generation) is int and 0 <= generation < 2**63 else 0
                state = row.get("state")
                task["state"] = state if state in _STOPPED else "paused"
                task["message"] = plain(row.get("message")) if state in _STOPPED else "上次下载已中断，可继续"
                raw_details = row.get("details")
                if isinstance(raw_details, dict):
                    task["details"] = self._persisted_details(raw_details)
                intent = row.get("stop")
                if intent in {"cancel", "remove"}:
                    task["state"], task["message"] = "cancelled", "下载已取消"
                    if same_directory:
                        task["partial"] = row.get("partial")
                        self._clear_partial(task)
                        self._clear_prepared(row.get("prepared"))
                    if intent == "remove":
                        self._journal.delete(key)
                        continue
                elif same_directory:
                    self._restore_files(task, row, state)
                elif task["state"] not in {"completed", "cancelled"}:
                    task["state"], task["message"] = "paused", "下载目录已更改，继续时将重新下载"
                self._tasks[key] = task
            except (KeyError, TypeError, ValueError, OSError, OverflowError):
                log.warning("download journal row ignored")

    def _restore_files(self, task, row, state):
        self._clear_prepared(row.get("prepared"))
        final = row.get("final")
        if self._safe_final(final) and task["state"] == "completed":
            task["path"] = str(Path(self.download_dir) / final)
            size = task["details"].get("size_bytes")
            if type(size) is int and size > 0:
                task["bytes_written"] = task["total_bytes"] = size
        pending = row.get("pending")
        if state == "saving" and isinstance(pending, dict) and self._safe_final(pending.get("name")):
            try:
                path = self._directory() / pending["name"]
                if self._file_stamp(path) == pending.get("stamp") and task["details"].get("media_evidence") in {"decoded", "metadata_parsed"}:
                    task.update(state="completed", message="上次已保存", path=str(path),
                                bytes_written=pending["stamp"][2], total_bytes=pending["stamp"][2])
            except OSError:
                pass
        if task["state"] in {"paused", "failed"} and isinstance(row.get("partial"), dict):
            task["partial"] = row["partial"]
            try:
                _, checkpoint = self._recover_part(task)
                if checkpoint:
                    task.update(bytes_written=checkpoint.bytes_written, total_bytes=checkpoint.total_bytes)
            except (OSError, ValueError, TypeError):
                # A record never grants permission to open or delete an unrelated file.
                task.pop("partial", None)

    def verified_options(self, song):
        """Media observations for this exact source ID, valid for this session."""
        with self._lock:
            return tuple(self._verified.get((song.get("source_name"), str(song.get("track_id"))), ()))

    def _remember_verified(self, track, requested, info):
        identity = track.source_name, str(track.track_id)
        option = (requested, info["extension"], info["quality"])
        with self._lock:
            if identity not in self._verified and len(self._verified) >= 128:
                self._verified.pop(next(iter(self._verified)))
            choices = self._verified.setdefault(identity, [])
            if option not in choices:
                choices.append(option)
                del choices[:-12]

    def download(self, song, callbacks=None, *, options=None) -> bool:
        cb = callbacks or {}
        choice = options.get("representation") if isinstance(options, dict) else None
        try:
            choice = self._stored_choice(asdict(choice)) if isinstance(choice, DownloadOption) else None
        except (TypeError, ValueError):
            self._call(cb, "on_fail", self._key(song.get("title"), song.get("artist")), "所选音质或格式暂不可用")
            return False
        request = dict(freeze_download_song(song), _download_options=normalize_options(options), _download_choice=choice)
        key = self.task_key(request, options)
        legacy_key = self._key(request.get("title"), request.get("artist"))
        failure = ""
        with self._lock:
            if self._closed.is_set():
                failure = "下载服务已关闭"
            elif key in self._tasks:
                return False
            elif len(self._tasks) >= MAX_RECORDS:
                failure = "下载任务已满，请移除旧记录"
            else:
                path = self.find_download_path(request.get("title"), request.get("artist"))
                if self._journal or not path or any(task["state"] == "completed" and task["path"] == path
                                   for task in self._tasks.values()):
                    task = self._empty_task(key, request)
                    self._tasks[key] = task
                    try:
                        self._schedule(task, cb)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        self._tasks.pop(key, None)
                        failure = "无法保存下载任务"
                    else:
                        return True
        self._call(cb, "on_fail" if failure else "on_exists", legacy_key, *([failure] if failure else []))
        return False

    def _schedule(self, task, cb):
        if self._closed.is_set():
            raise RuntimeError("Download manager closed")
        old = task.copy()
        task.update(state="queued", message="", generation=task["generation"] + 1, speed_bps=0.0, eta_seconds=None)
        run = _Attempt(task["generation"], cb)
        key = task["key"]
        self._runs[key] = run
        try:
            self._persist(task)
            run.future = self._pool.submit(self._run, task, run)
        except Exception:
            task.clear()
            task.update(old)
            self._runs.pop(key, None)
            raise

    @staticmethod
    def _call(callbacks, name, *args):
        callback = callbacks.get(name)
        if callback:
            try:
                callback(*args)
            except Exception as exc:
                log.warning("download callback failed type=%s", type(exc).__name__)

    def _legacy_event(self, task, run, name, *args):
        with self._lock:
            if self._tasks.get(task["key"]) is not task or task["generation"] != run.generation:
                return
            if run.action and name not in {"on_cancel"}:
                return
        request = task["request"]
        self._call(run.callbacks, name, self._key(request.get("title"), request.get("artist")), *args)

    def pause(self, key):
        return self._stop(key, "pause")

    def cancel(self, key):
        return self._stop(key, "cancel")

    def remove(self, key):
        return self._stop(key, "remove")

    def resume(self, key):
        return self._restart(key, "paused")

    def retry(self, key, callbacks=None):
        return self._restart(key, "failed", callbacks)

    def _restart(self, key, state, callbacks=None):
        with self._lock:
            task = self._tasks.get(key)
            if not task or task["state"] != state or key in self._runs or self._closed.is_set():
                return False
            try:
                self._schedule(task, callbacks or {})
            except (OSError, RuntimeError, ValueError, TypeError):
                task["message"] = "无法保存下载任务"
                return False
            return True

    def _stop(self, key, action):
        notify = None
        with self._lock:
            task = self._tasks.get(key)
            if not task:
                return False
            run = self._runs.get(key)
            priority = {"": 0, "pause": 1, "cancel": 2, "remove": 3}
            if run and (run.published or priority[action] <= priority[run.action]):
                return False
            if not run and (action == "pause" or (action == "cancel" and task["state"] not in {"paused", "failed"})):
                return False
            if run:
                next_state = "pausing" if action == "pause" else "cancelling"
                prior_state = task["state"]
                previous_intent = task.get("stop")
                task["state"] = next_state
                task["stop"] = action
                try:
                    self._persist(task)
                except (OSError, ValueError):
                    task["state"] = prior_state
                    task["stop"] = previous_intent
                    return False
                run.action = action
                run.event.set()
                if run.started or not run.future.cancel():
                    return True
                notify = run
            try:
                self._settle_stop(task, action)
            except (OSError, ValueError):
                # Keep the task addressable if its journal cannot be updated.
                task.update(state="failed", message="无法更新下载记录")
                return False
            finally:
                if notify:
                    self._runs.pop(key, None)
        if notify and action != "pause":
            self._legacy_event(task, notify, "on_cancel", "下载已取消")
        return True

    def _settle_stop(self, task, action):
        if action == "remove":
            self._clear_partial(task)
            if self._journal:
                self._journal.delete(task["key"])
            self._tasks.pop(task["key"], None)
        else:
            if action == "cancel":
                self._clear_partial(task)
            task.pop("stop", None)
            state, message = ("paused", "已暂停") if action == "pause" else ("cancelled", "下载已取消")
            task.update(state=state, message=message, speed_bps=0.0, eta_seconds=None)
            self._persist(task)

    def find_download_path_for_song(self, song):
        title, artist = song.get("title"), song.get("artist")
        source, native = song.get("source_name"), song.get("track_id")
        with self._lock:
            matching = [task for task in self._tasks.values() if task["state"] == "completed"
                        and self._key(task["request"].get("title"), task["request"].get("artist")) == self._key(title, artist)]
            if not matching:
                return self.find_download_path(title, artist)
            paths = {task["path"] for task in matching if task["path"] and source and native
                     and task["request"].get("source_name") == source
                     and str(task["request"].get("track_id")) == str(native)}
            if len(paths) == 1:
                path = Path(next(iter(paths)))
                try:
                    if path.parent == self._directory() and self._file_stamp(path):
                        return str(path)
                except OSError:
                    pass
        return ""

    def remove_download(self, title, artist, *, song=None, expected_path=None) -> tuple:
        with self._lock:
            path = self.find_download_path_for_song(song) if song is not None else self.find_download_path(title, artist)
            if not path or (expected_path is not None and path != expected_path):
                return False, "", "未找到已确认的下载文件"
            try:
                with pin_directory(self._directory()):
                    self._file_stamp(Path(path))
                    os.remove(path)
            except OSError:
                return False, path, "无法移除文件"
            for key, task in list(self._tasks.items()):
                if task["state"] == "completed" and task["path"] == path:
                    try:
                        self._settle_stop(task, "remove")
                    except OSError:
                        log.warning("completed copy removed, download record could not be removed")
            return True, path, ""

    def active_count(self):
        with self._lock:
            return self._active

    def _run(self, task, run):
        key = task["key"]
        with self._lock:
            if self._runs.get(key) is not run:
                return
            run.started = True
            self._active += 1
        self._current.run = run
        self._current.task = task
        error = ""
        outcome = None
        try:
            if not self._stopping():
                with pin_directory(self._directory(create=True)):
                    outcome = self._do_download(task["request"], key, self._worker_callbacks(task, run))
        except OSError as exc:
            log.warning("download storage error type=%s errno=%s", type(exc).__name__, exc.errno)
            error = "下载目录空间不足" if exc.errno == errno.ENOSPC else "无法写入下载目录"
        except Exception as exc:
            log.warning("download failed type=%s", type(exc).__name__)
            error = "下载暂不可用"
        finally:
            self._current.run = self._current.task = None
            with self._lock:
                self._active -= 1
                name, args = self._settle_attempt(task, run, outcome, error)
                self._runs.pop(key, None)
                self._close_journal_if_idle()
        if name:
            if name == "on_done":
                self._legacy_event(task, run, "on_details", dict(task["details"]))
            self._legacy_event(task, run, name, *args)

    def _settle_attempt(self, task, run, outcome, error):
        name, args = None, ()
        try:
            if run.published:
                final, detail = outcome or (task["path"], task["details"])
                self._clear_partial(task)
                task.update(state="completed", message="", path=final, details=detail,
                            bytes_written=detail["size_bytes"], total_bytes=detail["size_bytes"])
                task.pop("pending", None)
                self._persist(task)
                name, args = "on_done", (final,)
            elif run.action:
                self._settle_stop(task, run.action)
                if run.action != "pause" or self._closed.is_set():
                    name, args = "on_cancel", ("下载已取消",)
            else:
                message = error or task["message"] or "下载暂不可用"
                task.update(state="failed", message=message)
                self._persist(task)
                name, args = ("on_exists", ()) if task.pop("exists", False) else ("on_fail", (message,))
        except (OSError, ValueError) as exc:
            log.warning("download record could not be finalized type=%s", type(exc).__name__)
            if run.published:
                name, args = "on_done", (task["path"],)
            else:
                task.update(state="failed", message="无法更新下载记录")
                name, args = "on_fail", (task["message"],)
        task.update(speed_bps=0.0, eta_seconds=None)
        return name, args

    def _stopping(self):
        run = getattr(self._current, "run", None)
        return self._closed.is_set() or bool(run and run.event.is_set())

    def _state(self, task, run, state):
        with self._lock:
            if not run.event.is_set() and self._runs.get(task["key"]) is run:
                task["state"] = state
                if state != "downloading":
                    task.update(speed_bps=0.0, eta_seconds=None)

    def _worker_callbacks(self, task, run):
        states = {"正在检查可用音频": "probing", "正在校验音频": "validating", "正在保存": "saving"}

        def stage(key, message):
            self._state(task, run, states[message])
            self._legacy_event(task, run, "on_stage", message)

        return {
            "on_stage": stage,
            "on_progress": lambda key, progress: self._legacy_event(task, run, "on_progress", progress),
            "on_fail": lambda key, message: task.update(message=message),
            "on_exists": lambda key: task.update(exists=True, message="目标已有文件"),
        }

    def _do_download(self, song, key, cb):
        from . import download_assets as assets
        task, run = self._current.task, self._current.run
        options = song.get("_download_options", DEFAULT_DOWNLOAD_OPTIONS)
        if self._stopping():
            cb.get("on_fail", lambda k, r: None)(key, "下载已取消")
            return
        self._directory(create=True)
        from .online.download_probe import DownloadTransportError

        self._state(task, run, "resolving")
        offers, expected_priority = self._discover_offers(
            song, self._stopping, options=options, choice=song.get("_download_choice"),
            on_probe=lambda: cb.get("on_stage", lambda k, stage: None)(key, "正在检查可用音频"))
        if task.get("partial"):
            resumed = tuple(task["partial"].get("resource", ()))
            offers.sort(key=lambda offer: offer.option.resource == resumed, reverse=True)
        best = None
        rejected_format = False
        choice = song.get("_download_choice")
        strict_choice = choice or options.get("verified_quality") or options.get("format")
        try:
            transfers = 0
            last_failure = ""
            for offer in offers:
                if self._stopping():
                    break
                track, result, option = offer.track, offer.result, offer.option
                if best and not strict_choice and best[2].get("lossless") and option.codec:
                    if codec_family(option.codec) not in LOSSLESS_CODECS or (
                        option.bit_depth and option.sample_rate and option.score <= tuple(best[2]["score"])
                    ):
                        continue
                if options.get("format") and option.extension and options["format"] != option.extension:
                    rejected_format = True
                    continue
                if transfers >= MAX_TRANSFERS:
                    break
                transfers += 1
                part = None
                try:
                    self._state(task, run, "downloading")
                    try:
                        part, info, receipt = self._fetch_audio(track, result, key, cb, native=option.native)
                    except DownloadTransportError as exc:
                        if self._stopping():
                            raise
                        if exc.status in _REFRESH_HTTP:
                            result = self._resolve_rendition(track, option.native, 1.2)
                            if not result:
                                raise
                        elif self._retryable_transfer(task, exc):
                            self._state(task, run, "retrying")
                            if run.event.wait(.35):
                                raise InterruptedError("Download interrupted") from exc
                        else:
                            raise
                        self._state(task, run, "downloading")
                        part, info, receipt = self._fetch_audio(track, result, key, cb, native=option.native)
                    self._remember_verified(track, option.tier or "best", info)
                    if (options.get("format", info["extension"]) != info["extension"]
                            or options.get("verified_quality", info["quality"]) != info["quality"]
                            or (choice and not choice.matches(info))
                            or (not choice and options["quality"] != "best"
                                and not self._meets_explicit_tier(options["quality"], info))
                            or (not options["allow_fallback"] and not choice
                                and not self._meets_declared_floor(offer.priority, info))):
                        rejected_format = True
                        continue
                    if best is None or tuple(info["score"]) > tuple(best[2]["score"]):
                        if best:
                            self._forget_prepared(task, best[4])
                        best = part, track, info, offer, receipt
                        part = None
                    if choice or options.get("verified_quality"):
                        break
                except InterruptedError:
                    break
                except OSError:
                    raise
                except DownloadTransportError as exc:
                    last_failure = "音频传输不完整" if exc.reason in {"invalid_range", "empty_or_incomplete"} else "下载连接暂不可用"
                    log.info("download candidate unavailable reason=%s", exc.reason)
                except Exception as exc:
                    log.warning("download candidate rejected type=%s", type(exc).__name__)
                finally:
                    if part:
                        self._forget_prepared(task, receipt)
            if self._stopping():
                cb.get("on_fail", lambda k, r: None)(key, "下载已取消")
                return
            if best is None:
                reason = "所选音质或格式暂不可用" if rejected_format or strict_choice else (
                    "来源暂未提供可下载音频" if not offers else "音频未能完整下载")
                cb.get("on_fail", lambda k, r: None)(key, last_failure if last_failure and not strict_choice else reason)
                return
            part, track, info, selected, receipt = best
            base = self._filename_base(song.get("title"), song.get("artist"))
            filename = base + info["extension"]
            lowered = (not choice and options["quality"] == "best" and
                       (selected.priority < expected_priority or
                        not self._meets_declared_floor(expected_priority, info)))
            details = dict(quality=info["quality"], lyrics="not_requested", cover="not_requested",
                           metadata="not_requested", fallback=lowered,
                           requested_quality=options["quality"], provider_tier=selected.option.native,
                           declared_quality=selected.option.declared_quality,
                           availability_evidence=selected.evidence,
                           media_evidence=info.get("evidence"), verified=info.get("evidence") == "decoded", codec=info.get("codec"),
                           bitrate=info.get("bitrate"), sample_rate=info.get("sample_rate"),
                           bit_depth=info.get("bits_per_sample"), channels=info.get("channels"))
            cb.get("on_stage", lambda k, stage: None)(key, "正在保存")
            lyric = cover = None
            for name, reader in (("lyrics", lambda: assets.lyric_for_track(self.online_manager, track)),
                                 ("cover", lambda: assets.cover_for_track(track, cancelled=self._stopping))):
                if options[name]:
                    try: value = reader()
                    except Exception as exc:
                        log.warning("download attachment unavailable kind=%s type=%s", name, type(exc).__name__)
                        value = None
                    details[name] = "unavailable" if not value else "ready"
                    if name == "lyrics": lyric = value
                    else: cover = value
            if self._stopping():
                return
            if options["metadata"]:
                if self._file_stamp(part) != receipt["stamp"]:
                    raise OSError("Validated temporary file changed")
                try:
                    with self._open_owned(part, receipt["stamp"]) as stream:
                        assets.embed_tags(part, track, lyric, cover, stream=stream)
                    details["metadata"] = "saved"
                except Exception as exc:
                    log.warning("download tags unavailable type=%s", type(exc).__name__)
                    details["metadata"] = "unavailable"
                if not self._stopping():
                    stamp = self._file_stamp(part)
                    tagged = self._inspect_audio(part)
                    if not isinstance(tagged, dict) or tagged.get("score") != info["score"]:
                        raise ValueError("Tag write damaged audio")
                    if self._file_stamp(part) != stamp or stamp[:2] != receipt["stamp"][:2]:
                        raise OSError("Tagged temporary file changed")
                    receipt["stamp"] = stamp
            if self._stopping():
                cb.get("on_fail", lambda k, r: None)(key, "下载已取消")
                return
            details["format"] = info["extension"].lstrip(".").upper()
            try:
                final = self._publish(part, filename, task, run, details, receipt)
            except FileExistsError:
                cb.get("on_exists", lambda k: None)(key)
                return
            if not final:
                return
            for name, path, content in (("lyrics", os.path.splitext(final)[0]+".lrc", lyric.encode("utf-8") if lyric else None),
                                         ("cover", os.path.splitext(final)[0]+(cover[2] if cover else ".jpg"), cover[0] if cover else None)):
                if content:
                    try: details[name] = assets.save_sidecar(path, content)
                    except Exception: details[name] = "unavailable"
            details.update(format=info["extension"].lstrip(".").upper(), size_bytes=os.path.getsize(final), indexed=True)
            try:
                self.db.insert_song(dict(path=final, filename=os.path.basename(final), title=track.title,
                    artist=track.artist, album=track.album, duration=info["duration"],
                    format=info["extension"].lstrip("."), filesize=details["size_bytes"], last_modified=time.time()))
            except Exception:
                details["indexed"] = False
                log.exception("download indexing failed")
            return final, details
        finally:
            if best:
                self._forget_prepared(task, best[4])

    @staticmethod
    def _retryable_transfer(task, error):
        if error.status is not None and 500 <= error.status < 600:
            return True
        if error.reason not in {"timeout", "network"}:
            return False
        data = (task.get("partial") or {}).get("checkpoint") or {}
        # Avoid repeating a full body once bytes were received without a way
        # to verify and reuse them. Continue testing other legitimate offers.
        return not data.get("bytes_written") or bool(data.get("etag"))

    @staticmethod
    def _stamp(info):
        if not stat.S_ISREG(info.st_mode) or is_link(info) or info.st_nlink != 1:
            raise OSError("Unsafe download file")
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]

    def _file_stamp(self, path):
        return self._stamp(path.lstat())

    def _recover_part(self, task, resource=None):
        from .online.download_probe import TransferCheckpoint

        partial = task.get("partial")
        if not partial:
            return None, None
        if not isinstance(partial, dict):
            raise ValueError("Invalid partial record")
        name = partial.get("name")
        if not isinstance(name, str) or not _PART.fullmatch(name):
            raise ValueError("Invalid partial name")
        path = self._directory() / name
        expected = partial.get("stamp")
        stamp = self._file_stamp(path)
        if not isinstance(expected, list) or len(expected) != 4 or expected[:2] != stamp[:2]:
            raise ValueError("Invalid partial identity")
        if resource is not None and list(resource) != partial.get("resource"):
            return path, None
        data = partial.get("checkpoint")
        if not isinstance(data, dict) or stamp != expected:
            return path, None
        checkpoint = TransferCheckpoint(**data)
        if (type(checkpoint.bytes_written) is not int or checkpoint.bytes_written <= 0
                or checkpoint.bytes_written != stamp[2]
                or (checkpoint.total_bytes is not None and
                    (type(checkpoint.total_bytes) is not int or checkpoint.total_bytes < stamp[2]))
                or not isinstance(checkpoint.resource_key, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checkpoint.resource_key)
                or any(value is not None and not plain(value) for value in (
                    checkpoint.etag, checkpoint.last_modified))):
            return path, None
        return path, checkpoint

    def _clear_partial(self, task):
        try:
            path, _ = self._recover_part(task)
            if path:
                with pin_directory(path.parent):
                    if self._file_stamp(path)[:2] == task["partial"]["stamp"][:2]:
                        path.unlink()
        except (OSError, ValueError, TypeError):
            pass
        task.pop("partial", None)

    def _clear_prepared(self, receipts):
        if not isinstance(receipts, list):
            return
        for entry in receipts[:MAX_TRANSFERS]:
            if isinstance(entry, dict):
                self._clear_partial({"partial": entry})

    def _forget_prepared(self, task, receipt):
        self._clear_prepared([receipt])
        with self._lock:
            prepared = task.get("prepared", [])
            if receipt in prepared:
                prepared.remove(receipt)
                try:
                    self._persist(task)
                except (OSError, ValueError):
                    log.warning("download temporary cleanup could not be recorded")

    def _new_part(self, task, resource):
        directory = self._directory()
        part = directory / f"starlight-{uuid.uuid4().hex}.part"
        with part.open("xb") as file:
            stamp = self._stamp(os.fstat(file.fileno()))
        task["partial"] = {"name": part.name, "stamp": stamp, "resource": list(resource)}
        try:
            with self._lock:
                self._persist(task)
        except Exception:
            self._clear_partial(task)
            raise
        return part

    def _open_owned(self, part, stamp, *, writable=True):
        flags = (os.O_RDWR if writable else os.O_RDONLY) | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        if self._directory() != part.parent:
            raise OSError("Unsafe partial directory")
        expected = stamp[:2]
        if self._file_stamp(part)[:2] != expected:
            raise OSError("Partial changed")
        fd = os.open(part, flags)
        try:
            if self._stamp(os.fstat(fd))[:2] != expected or self._file_stamp(part)[:2] != expected:
                raise OSError("Partial changed")
            return os.fdopen(fd, "r+b" if writable else "rb")
        except BaseException:
            os.close(fd)
            raise

    def _fetch_audio(self, track, result, key, callbacks, directory=None, cancelled=None, *, native=None):
        from .online.download_probe import DownloadTransportError

        task, run = self._current.task, self._current.run
        resource = (track.source_name, str(track.track_id), native)
        try:
            part, checkpoint = self._recover_part(task, resource)
        except (OSError, ValueError, TypeError):
            part, checkpoint = None, None
        if checkpoint is None:
            self._clear_partial(task)
            part = self._new_part(task, resource)

        def checkpoint_written(value):
            data = asdict(value)
            for name in ("etag", "last_modified"):
                if not plain(data[name]):
                    data[name] = None
            with self._lock:
                stamp = self._file_stamp(part)
                if stamp[:2] != task["partial"]["stamp"][:2]:
                    raise OSError("Partial changed")
                task["partial"].update(stamp=stamp, checkpoint=data)
                task.update(bytes_written=value.bytes_written, total_bytes=value.total_bytes)
                self._persist(task)

        previous = None

        def written(count, total):
            nonlocal previous
            now = time.monotonic()
            with self._lock:
                if self._runs.get(key) is not run or run.event.is_set():
                    return
                if previous is None or count < previous[0]:
                    speed = 0.0
                else:
                    delta = now - previous[1]
                    speed = (count - previous[0]) / delta if delta > 0 else 0.0
                previous = count, now
                task.update(bytes_written=count, total_bytes=total, speed_bps=speed,
                            eta_seconds=(total - count) / speed if total and count < total and speed > 0 else None)

        try:
            bytes_written = self._stream(
                result["url"], result.get("headers") or {}, part, key, callbacks,
                cancelled=cancelled, expected_size=result.get("_expected_size"),
                resume=checkpoint, checkpoint=checkpoint_written,
                open_part=lambda: self._open_owned(part, task["partial"]["stamp"]), on_bytes=written,
            )
            if bytes_written <= 0:
                raise ValueError("empty audio")
            if self._stopping():
                raise InterruptedError("Download interrupted")
            callbacks.get("on_stage", lambda k, stage: None)(key, "正在校验音频")
            stamp = self._file_stamp(part)
            if stamp[:2] != task["partial"]["stamp"][:2]:
                raise OSError("Partial changed")
            info = self._inspect_audio(part)
            if self._file_stamp(part) != stamp:
                raise OSError("Media changed during validation")
            if (not isinstance(info, dict) or info.get("extension") not in _EXT_HINTS
                    or not 0 < info.get("duration", 0) or not info.get("score")):
                raise ValueError("Missing media validation")
            expected = getattr(track, "duration_seconds", 0)
            if (type(expected) in (int, float) and expected > 45
                    and info["duration"] < expected - max(10, expected * 0.05)):
                raise ValueError("audio is shorter than the selected recording")
            with self._lock:
                # This complete temporary file may still be selected for saving.
                # A later candidate gets a different temporary identity.
                completed = task.pop("partial")
                completed["stamp"] = stamp
                task.setdefault("prepared", []).append(completed)
                self._persist(task)
            return part, info, completed
        except BaseException as exc:
            if (not self._stopping() and not isinstance(exc, DownloadTransportError)):
                self._clear_partial(task)
            raise

    def _publish(self, part, filename, task, run, details, receipt):
        from .download_assets import publish_new

        with self._lock:
            if self._stopping() or self._runs.get(task["key"]) is not run:
                return ""
            directory = self._directory()
            if part.parent != directory or self._file_stamp(part) != receipt["stamp"]:
                raise OSError("Validated temporary file changed")
            final = directory / filename
            if os.path.lexists(final):
                owner = next((row for row in self._tasks.values()
                              if row is not task and row["state"] == "completed"
                              and os.path.normcase(row["path"]) == os.path.normcase(str(final))), None)
                if owner is None and self._journal is None:
                    raise FileExistsError
                for suffix in range(2, MAX_RECORDS + 2):
                    final = directory / f"{Path(filename).stem} ({suffix}){Path(filename).suffix}"
                    if not os.path.lexists(final):
                        break
                else:
                    raise FileExistsError
            stamp = self._file_stamp(part)
            task["pending"] = {"name": final.name, "stamp": stamp}
            task["details"] = dict(details, size_bytes=stamp[2], indexed=False)
            self._persist(task)
            if self._file_stamp(part) != stamp:
                raise OSError("Validated temporary file changed")
            publish_new(part, final)
            run.published = True
            task["path"] = str(final)
            return str(final)

    def probe_options(self, song, callback, cancelled):
        """Report bounded current observations without creating a download file."""
        if self._closed.is_set() or not self._probe_slots.acquire(blocking=False):
            return False

        def cancelled_or_closed():
            return cancelled() or self._closed.is_set()

        def probe(frozen):
            try:
                found, _ = self._discover_offers(frozen, cancelled_or_closed, menu=True)
            except Exception as exc:
                log.info("download option check failed type=%s", type(exc).__name__)
                found = []
            if not cancelled_or_closed():
                callback(tuple(offer.option for offer in found
                               if offer.evidence == "reachable" and offer.option.extension))

        def released(future):
            with self._lock:
                self._probe_futures.discard(future)
                self._probe_slots.release()

        try:
            with self._lock:
                future = self._probe_pool.submit(probe, freeze_download_song(song))
                self._probe_futures.add(future)
                future.add_done_callback(released)
        except (RuntimeError, TypeError, ValueError):
            self._probe_slots.release()
            return False
        return True

    @staticmethod
    def _preferred_track(song):
        from .online.models import OnlineTrack
        if song.get("source_name") and song.get("track_id"):
            return OnlineTrack(song["source_name"], str(song["track_id"]), song.get("title") or "",
                               song.get("artist") or "", song.get("album") or "",
                               song.get("duration_seconds") or song.get("duration") or 0,
                               song.get("cover_url"), dict(song.get("extra") or {}))
        return None

    def _recording_candidates(self, song):
        from .online.models import OnlineTrack
        from .online.content_choices import equivalent_versions, same_recording
        from .online.popular_charts import normalized
        title, artist = song.get("title", ""), song.get("artist", "")
        if not title or self.online_manager is None:
            return []
        preferred = self._preferred_track(song)
        if preferred is None and not artist:
            return []
        supplied = song.get("_download_alternatives")
        try:
            found = self.online_manager.search_all(f"{title} {artist}", limit_per_source=5) if supplied is None else {}
            pool = (t for tracks in found.values() for t in tracks) if supplied is None else supplied
            found = [t for t in pool if isinstance(t, OnlineTrack)]
        except Exception:
            log.warning("download candidate search unavailable")
            found = []
        matched = [t for t in found if normalized(t.title)==normalized(title) and normalized(t.artist)==normalized(artist)]
        if preferred is not None:
            matched = [preferred, *equivalent_versions(preferred, matched)]
        elif len(matched) > 1 and not all(
            same_recording(left, right) for index, left in enumerate(matched) for right in matched[index + 1:]
        ):
            return []  # Ambiguous versions require the user to choose an actual result.
        return matched

    def _resolve_rendition(self, track, native, timeout):
        manager = self.online_manager
        method = getattr(type(manager), "get_download_rendition", None)
        try:
            if method is not None:
                return method(manager, track, native, timeout=timeout)
            # Compatibility for third-party manager implementations with a
            # single adapter. The formal manager always uses owner-only routing.
            result = manager.get_play_url(track, quality=native)
            return dict(result, requested_quality=native) if isinstance(result, dict) else None
        except Exception as exc:
            log.info("download address unavailable type=%s", type(exc).__name__)
            return None

    def _request_plan(self, tracks, options, choice):
        from .online.search_resources import search_resources

        manager = self.online_manager
        tiers = getattr(type(manager), "download_tiers", None)
        requested = options["quality"]
        planned = []
        for track in tracks:
            available = tiers(manager, track) if tiers else [(tier, tier) for tier in QUALITY_ORDER]
            hints = {item.declared_quality: item for item in search_resources(track)}
            for tier, native in available:
                if requested != "best" and not choice and tier != requested:
                    continue
                hint = hints.get(native) if native else hints.get(None)
                preference = TIER_ORDER.get(tier, 0)
                if hint and hint.format in ("FLAC", "ALAC", "APE"):
                    preference = max(preference, TIER_ORDER["lossless"])
                    if (hint.bit_depth or 0) >= 24 and (hint.sample_rate_hz or 0) > 48_000:
                        preference = TIER_ORDER["hires"]
                pinned = bool(choice and (track.source_name, str(track.track_id), native) == choice.resource)
                planned.append((pinned, preference, hint, track, tier, native))
        planned.sort(key=lambda item: (item[0], item[1], item[2] is not None), reverse=True)
        if requested == "best" and not options["allow_fallback"] and planned and not choice:
            highest_declared = max((item[1] for item in planned if item[2]), default=planned[0][1])
            planned = [item for item in planned if item[1] >= highest_declared]
        return planned

    def _discover_offers(self, song, cancelled, *, menu=False, options=None, choice=None, on_probe=None):
        from .online.download_probe import probe_download

        deadline = time.monotonic() + PROBE_SECONDS
        remaining_bytes = PROBE_BYTES
        tracks = self._recording_candidates(song)
        plans = self._request_plan(tracks, options or DEFAULT_DOWNLOAD_OPTIONS, choice)
        seen, offers = set(), []
        expected_priority = max((item[1] for item in plans if item[2]), default=0)
        probing = 0
        for _, priority, hint, track, tier, native in plans[:MAX_RESOLUTIONS]:
            remaining = deadline - time.monotonic()
            if cancelled() or remaining <= .05 or probing >= MAX_PROBES:
                break
            result = self._resolve_rendition(track, native, min(1.2, remaining))
            if not result or not isinstance(result.get("url"), str) or not result["url"]:
                continue
            headers = result.get("headers") or {}
            dedup = result["url"], tuple(sorted((str(k), str(v)) for k, v in headers.items()))
            if dedup in seen:
                continue
            seen.add(dedup)
            remaining = deadline - time.monotonic()
            if remaining <= .05 or cancelled():
                break
            probing += 1
            if on_probe:
                on_probe()
            proof = probe_download(result["url"], headers, timeout=min(1.4, remaining),
                                   max_bytes=min(8192, remaining_bytes), cancelled=cancelled)
            remaining_bytes -= proof.bytes_read
            if (proof.available is False and proof.http_status in _REFRESH_HTTP
                    and probing < MAX_PROBES and deadline - time.monotonic() > .25):
                refreshed = self._resolve_rendition(track, native, min(.8, deadline - time.monotonic()))
                if refreshed and refreshed.get("url") and (refreshed.get("url"), refreshed.get("headers")) != (
                    result["url"], result.get("headers")
                ):
                    result = refreshed
                    remaining = deadline - time.monotonic()
                    if remaining <= .05 or cancelled():
                        break
                    probing += 1
                    proof = probe_download(result["url"], result.get("headers"), timeout=min(1.3, remaining),
                                           max_bytes=min(8192, remaining_bytes), cancelled=cancelled)
                    remaining_bytes -= proof.bytes_read
            if (proof.available is False or proof.reason in {
                    "invalid_url", "blocked_target", "blocked_redirect", "invalid_redirect", "redirect_limit"
                } or (menu and proof.available is not True)):
                continue
            if proof.size_bytes and hint and hint.size_bytes and proof.size_bytes != hint.size_bytes:
                log.info("download resource size differs from source declaration")
            option = DownloadOption(
                track.source_name, str(track.track_id), tier, native,
                CONTAINERS.get(proof.container), proof.codec, proof.size_bytes or (hint.size_bytes if hint else None),
                proof.bitrate, proof.sample_rate, proof.bit_depth, proof.channels,
                proof.size_evidence if proof.size_bytes else "source_declared" if hint and hint.size_bytes else None,
                result.get("source_quality") or (hint.declared_quality if hint else None),
            )
            if proof.size_bytes:
                result = dict(result, _expected_size=proof.size_bytes)
            offers.append(DownloadOffer(track, result, option, priority,
                                        "reachable" if proof.available else "unknown"))
        offers.sort(key=lambda item: (bool(choice and item.option.resource == choice.resource),
                                      item.option.score, item.priority), reverse=True)
        return offers, expected_priority or max((item[1] for item in plans), default=0)

    @classmethod
    def _meets_explicit_tier(cls, tier, info):
        if tier in ("standard", "high"):
            return info["score"][0] >= (3 if tier == "high" else 2)
        return cls._meets_declared_floor(TIER_ORDER[tier], info)

    @staticmethod
    def _meets_declared_floor(priority, info):
        if priority >= TIER_ORDER["hires"]:
            return (bool(info.get("lossless")) and info["bits_per_sample"] >= 24
                    and info["sample_rate"] > 48_000)
        if priority >= TIER_ORDER["lossless"]:
            return bool(info.get("lossless"))
        return True

    def _inspect_audio(self, path):
        from .download_assets import inspect_audio
        task = getattr(self._current, "task", None)
        if not task:
            return inspect_audio(path, cancelled=self._stopping)
        owned = [*task.get("prepared", []), task.get("partial")]
        receipt = next((entry for entry in owned if entry and entry["name"] == path.name), None)
        if not receipt:
            raise OSError("Download file is not owned by this task")
        with self._open_owned(path, receipt["stamp"], writable=False) as stream:
            return inspect_audio(path, cancelled=self._stopping, stream=stream)

    def _stream(self, url, headers, part, key, cb, *, cancelled=None, expected_size=None, **transfer):
        from .online.download_probe import stream_download
        return stream_download(url, headers, part,
                               lambda progress: cb.get("on_progress", lambda k, p: None)(key, progress),
                               cancelled=lambda: self._stopping() or bool(cancelled and cancelled()),
                               expected_size=expected_size, **transfer)

    def _close_journal_if_idle(self):
        if self._journal and self._closed.is_set() and not self._runs:
            self._journal.close()

    def close(self):
        cancelled = []
        with self._lock:
            if self._closed.is_set():
                return
            self._closed.set()
            for key, run in list(self._runs.items()):
                if run.published:
                    continue
                task = self._tasks[key]
                if not run.action:
                    run.action = "pause" if self._journal else "cancel"
                run.event.set()
                if not run.started and run.future.cancel():
                    try:
                        self._settle_stop(task, run.action)
                    except OSError:
                        task.update(state="failed", message="无法更新下载记录")
                    self._runs.pop(key, None)
                    cancelled.append((task, run))
            self._close_journal_if_idle()
        self._probe_pool.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False, cancel_futures=True)
        for task, run in cancelled:
            self._legacy_event(task, run, "on_cancel", "下载已取消")

    def wait_closed(self, timeout):
        from concurrent.futures import wait
        with self._lock:
            pending = [*(run.future for run in self._runs.values()), *self._probe_futures]
        return not wait(pending, timeout=timeout).not_done
