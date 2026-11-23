"""Bounded background downloads with conservative recording identity and media checks.

Try available quality tiers, compare inspected media, attach matching assets, then
publish without replacing existing user files. No player instance is involved.
"""

import errno
import os
import logging
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

_EXT_HINTS = (".flac", ".mp3", ".m4a", ".ogg", ".wav", ".ape")
log = logging.getLogger(__name__)
DEFAULT_DOWNLOAD_OPTIONS = dict(quality="best", lyrics=True, cover=True, metadata=True)
QUALITY_ORDER = ("super", "high", "standard", "low")


def normalize_options(options=None):
    result = dict(DEFAULT_DOWNLOAD_OPTIONS)
    if isinstance(options, dict):
        if options.get("quality") in ("best", *QUALITY_ORDER):
            result["quality"] = options["quality"]
        if options.get("format") in _EXT_HINTS:
            result["format"] = options["format"]
        if isinstance(options.get("verified_quality"), str) and options["verified_quality"]:
            result["verified_quality"] = options["verified_quality"]
        for key in ("lyrics", "cover", "metadata"):
            if type(options.get(key)) is bool:
                result[key] = options[key]
    return result


_ILLEGAL = '\\/:*?"<>|'


class DownloadManager:
    """并发受限的在线歌下载器。线程安全。"""

    def __init__(self, online_manager, db, download_dir, max_workers=3):
        self.online_manager = online_manager
        self.db = db
        self.download_dir = download_dir
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="music-dl")
        self._lock = threading.Lock()
        self._closed = threading.Event()
        self._active = 0
        self._verified = {}
        self._tasks = {}  # active only: queued/downloading

    # ---------- 判定 / 文件名 ----------

    @staticmethod
    def _key(title, artist):
        return f"{title or ''}|{artist or ''}".strip("|")

    @staticmethod
    def _sanitize(name: str) -> str:
        for ch in _ILLEGAL:
            name = name.replace(ch, "_")
        return name.strip()

    def _filename_base(self, title, artist) -> str:
        t = self._sanitize(title or "未知")
        a = self._sanitize(artist or "")
        return f"{t} - {a}" if a else t

    def is_downloaded(self, title, artist) -> bool:
        """下载目录里有 歌名 - 歌手.<任一音频扩展> → 已下载。"""
        base = self._filename_base(title, artist)
        if not base or not os.path.isdir(self.download_dir):
            return False
        for ext in _EXT_HINTS:
            if os.path.exists(os.path.join(self.download_dir, base + ext)):
                return True
        return False

    def find_download_path(self, title, artist) -> str:
        """返回已下载文件的完整路径；无 → ""。"""
        base = self._filename_base(title, artist)
        if not base or not os.path.isdir(self.download_dir):
            return ""
        for ext in _EXT_HINTS:
            p = os.path.join(self.download_dir, base + ext)
            if os.path.exists(p):
                return p
        return ""

    # ---------- 下载入口 ----------

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
        title, artist = song.get("title", ""), song.get("artist", "")
        key, cb = self._key(title, artist), callbacks or {}
        options = normalize_options(options)
        with self._lock:
            closed = self._closed.is_set()
            cur = self._tasks.get(key)
            if not closed and cur and cur["status"] in ("queued", "downloading"):
                return False
            exists = not closed and self.is_downloaded(title, artist)
            if not closed and not exists:
                self._tasks[key] = {"status": "queued"}
        if closed:
            cb.get("on_cancel", cb.get("on_fail", lambda k, reason: None))(key, "下载已取消")
            return False
        if exists:
            cb.get("on_exists", lambda k: None)(key)
            return False
        request = dict(song, _download_options=options)
        try:
            future = self._pool.submit(self._run, request, key, cb)
            future.add_done_callback(lambda completed: self._cancel_queued(completed, key, cb))
        except RuntimeError:
            self._fail(key, cb, "下载服务已关闭")
            return False
        return True

    def _cancel_queued(self, future, key, cb):
        if future.cancelled():
            self._fail(key, cb, "下载已取消")

    def remove_download(self, title, artist) -> tuple:
        """删下载目录里的 歌名 - 歌手.* 文件。返回 (成功?, 路径, 错误信息)。"""
        path = self.find_download_path(title, artist)
        if not path:
            return False, "", "未找到已下载文件"
        try:
            os.remove(path)
            return True, path, ""
        except Exception as e:
            return False, path, str(e)

    # ---------- 内部 ----------

    def active_count(self) -> int:
        """当前运行中的下载数（测试/状态查询用）。"""
        with self._lock:
            return self._active

    def _run(self, song, key, cb):
        with self._lock:
            self._active += 1
        try:
            try:
                self._do_download(song, key, cb)
            except OSError as exc:
                log.warning("download storage error type=%s errno=%s", type(exc).__name__, exc.errno)
                if exc.errno == errno.ENOSPC:
                    reason = "下载目录空间不足"
                else:
                    reason = "无法写入下载目录"
                self._fail(key, cb, reason)
            except Exception as exc:
                log.warning("download failed type=%s", type(exc).__name__)
                self._fail(key, cb, "下载暂不可用")
        finally:
            with self._lock:
                self._active -= 1

    def _do_download(self, song, key, cb):
        from . import download_assets as assets
        options = song.get("_download_options", DEFAULT_DOWNLOAD_OPTIONS)
        if self._closed.is_set():
            self._fail(key, cb, "下载已取消")
            return
        os.makedirs(self.download_dir, exist_ok=True)
        candidates = self._resolve_candidates(song)
        best = None
        rejected_format = False
        seen_urls = set()
        strict_choice = options.get("verified_quality") or options.get("format")
        try:
            for track, result in candidates:
                while result and not self._closed.is_set():
                    if result["url"] in seen_urls:
                        result = self._next_quality(track, result)
                        continue
                    seen_urls.add(result["url"])
                    valid = False
                    part = None
                    try:
                        with self._lock:
                            self._tasks[key]["status"] = "downloading"
                        part, info = self._fetch_audio(track, result, key, cb, self.download_dir)
                        self._remember_verified(track, result["_requested_quality"], info)
                        valid = (
                            options.get("format", info["extension"]) == info["extension"]
                            and options.get("verified_quality", info["quality"]) == info["quality"]
                        )
                        if not valid:
                            rejected_format = True
                        if valid and (best is None or tuple(info["score"]) > tuple(best[2]["score"])):
                            if best: self._cleanup(best[0])
                            best = (part, track, info)
                            part = None
                    except Exception as exc:
                        log.warning("download candidate rejected type=%s", type(exc).__name__)
                    finally:
                        if part: self._cleanup(part)
                    if valid or options.get("verified_quality"):
                        break
                    result = self._next_quality(track, result)
                    while result and result["url"] in seen_urls:
                        result = self._next_quality(track, result)
            if self._closed.is_set():
                self._fail(key, cb, "下载已取消")
                return
            if best is None:
                reason = "所选音质或格式暂不可用" if rejected_format or strict_choice else (
                    "来源暂未提供可下载音频" if not candidates else "音频未能完整下载")
                self._fail(key, cb, reason)
                return
            part, track, info = best
            base = self._filename_base(track.title, track.artist)
            final = os.path.join(self.download_dir, base + info["extension"])
            details = dict(quality=info["quality"], lyrics="not_requested", cover="not_requested", metadata="not_requested")
            cb.get("on_stage", lambda k, stage: None)(key, "正在保存")
            lyric = cover = None
            for name, reader in (("lyrics", lambda: assets.lyric_for_track(self.online_manager, track)),
                                 ("cover", lambda: assets.cover_for_track(track))):
                if options[name]:
                    try: value = reader()
                    except Exception as exc:
                        log.warning("download attachment unavailable kind=%s type=%s", name, type(exc).__name__)
                        value = None
                    details[name] = "unavailable" if not value else "ready"
                    if name == "lyrics": lyric = value
                    else: cover = value
            if options["metadata"]:
                try:
                    assets.embed_tags(part, track, lyric, cover)
                    details["metadata"] = "saved"
                except Exception as exc:
                    log.warning("download tags unavailable type=%s", type(exc).__name__)
                    details["metadata"] = "unavailable"
            if self._closed.is_set():
                self._fail(key, cb, "下载已取消")
                return
            try:
                assets.publish_new(part, final)
            except FileExistsError:
                cb.get("on_exists", lambda k:None)(key)
                with self._lock: self._tasks.pop(key, None)
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
            with self._lock: self._tasks.pop(key, None)
            cb.get("on_details", lambda k,d:None)(key, details)
            cb.get("on_done", lambda k,p:None)(key, final)
        finally:
            if best: self._cleanup(best[0])

    def _fetch_audio(self, track, result, key, callbacks, directory=None, cancelled=None):
        fd, part = tempfile.mkstemp(prefix="starlight-", suffix=".part", dir=directory)
        os.close(fd)
        try:
            if self._stream(result["url"], result.get("headers") or {}, part, key, callbacks,
                            **({"cancelled": cancelled} if cancelled is not None else {})) <= 0:
                raise ValueError("empty audio")
            callbacks.get("on_stage", lambda k, stage: None)(key, "正在校验音频")
            info = self._inspect_audio(part)
            expected = getattr(track, "duration_seconds", 0)
            if (type(expected) in (int, float) and expected > 45
                    and info["duration"] < expected - max(10, expected * 0.05)):
                raise ValueError("audio is shorter than the selected recording")
            return part, info
        except Exception:
            self._cleanup(part)
            raise

    def probe_options(self, song, callback, cancelled):
        """Verify on demand using the same transfer and media validation as a download."""
        track = self._preferred_track(song)
        if track is None or self._closed.is_set():
            return False

        def probe():
            seen = set()
            for requested in QUALITY_ORDER:
                if cancelled() or self._closed.is_set():
                    return
                result = self._get_url_with_fallback(track, requested, exact=True)
                if not result or result["url"] in seen:
                    continue
                seen.add(result["url"])
                try:
                    part, info = self._fetch_audio(track, result, "", {}, cancelled=cancelled)
                except Exception as exc:
                    log.info("download option rejected type=%s", type(exc).__name__)
                else:
                    self._cleanup(part)
                    if not cancelled() and not self._closed.is_set():
                        self._remember_verified(track, requested, info)
            if not cancelled():
                callback(self.verified_options(song))

        try:
            self._pool.submit(probe)
        except RuntimeError:
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

    def _resolve_track(self, song):
        candidates = self._resolve_candidates(song)
        if candidates:
            track, result = candidates[0]
            return track, result["url"], result.get("headers") or {}
        return None

    def _resolve_candidates(self, song):
        from .online.models import OnlineTrack
        from .online.content_choices import recording_key
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
            identity = recording_key(preferred)
            matched = [preferred] + [t for t in matched if identity is not None and recording_key(t)==identity]
        elif len(matched) > 1:
            identities = {recording_key(t) for t in matched}
            if None in identities or len(identities) != 1:
                return []  # Ambiguous versions require the user to choose an actual result.
        seen, candidates = set(), []
        options = song.get("_download_options", DEFAULT_DOWNLOAD_OPTIONS)
        quality = options["quality"]
        for track in matched:
            if self._closed.is_set(): break
            identity = track.source_name, track.track_id
            if identity in seen: continue
            seen.add(identity)
            if len(seen) > 3:
                break
            result = self._get_url_with_fallback(track, quality, exact=bool(options.get("verified_quality")))
            if result and result.get("url"):
                candidates.append((track,result))
        return candidates

    def _inspect_audio(self, path):
        from .download_assets import inspect_audio
        return inspect_audio(path, cancelled=self._closed.is_set)

    def _get_url_with_fallback(self, track, quality="best", *, exact=False):
        qualities = QUALITY_ORDER if quality == "best" else QUALITY_ORDER[QUALITY_ORDER.index(quality):]
        for requested in qualities[:1] if exact else qualities:
            if self._closed.is_set(): return None
            try:
                result = self.online_manager.get_play_url(track, quality=requested)
                if result and result.get("url"):
                    return dict(result, _requested_quality=requested)
            except Exception:
                continue
        return None

    def _next_quality(self, track, result):
        current = result.get("_requested_quality", "low")
        index = QUALITY_ORDER.index(current)
        if self._closed.is_set() or index + 1 >= len(QUALITY_ORDER):
            return None
        return self._get_url_with_fallback(track, QUALITY_ORDER[index + 1])

    def _stream(self, url, headers, part, key, cb, *, cancelled=None):
        """流式下载到 .part；有 Content-Length → 百分比进度，无 → on_progress(key, None)。"""
        h = {"User-Agent": "Mozilla/5.0"}
        for k in ("Referer", "Cookie", "Authorization"):
            if headers and headers.get(k):
                h[k] = headers[k]
        req = Request(url, headers=h)
        last_progress_at = 0.0
        with urlopen(req, timeout=30) as resp:
            total_s = resp.headers.get("Content-Length")
            total = int(total_s) if total_s and total_s.isdigit() else None
            got = 0
            with open(part, "wb") as f:
                while True:
                    if self._closed.is_set() or (cancelled and cancelled()):
                        raise InterruptedError("download cancelled")
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    now = time.monotonic()
                    if now - last_progress_at >= 0.2:
                        pct = min(99, int(got * 100 / total)) if total else None
                        cb.get("on_progress", lambda k, p: None)(key, pct)
                        last_progress_at = now
        if total is not None and got != total:
            raise ValueError("incomplete audio response")
        return got

    def _fail(self, key, cb, reason):
        with self._lock:
            self._tasks.pop(key, None)
        log.warning("download failed key=%s reason=%s", key, reason)
        cb.get("on_cancel" if self._closed.is_set() else "on_fail", cb.get("on_fail", lambda k,r:None))(key, reason)

    @staticmethod
    def _cleanup(path):
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

    def close(self):
        self._closed.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
