"""Bounded background downloads with conservative recording identity and media checks.

Try available quality tiers, compare inspected media, attach matching assets, then
publish without replacing existing user files. No player instance is involved.
"""

import errno
import os
import logging
import tempfile
import threading
from copy import deepcopy
from dataclasses import replace
import time
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
        if isinstance(options.get("verified_quality"), str) and options["verified_quality"]:
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

    def __init__(self, online_manager, db, download_dir, max_workers=3):
        self.online_manager = online_manager
        self.db = db
        self.download_dir = download_dir
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="music-dl")
        self._probe_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="music-dl-probe")
        self._probe_slots = threading.BoundedSemaphore(2)
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
        choice = options.get("representation") if isinstance(options, dict) else None
        choice = choice if isinstance(choice, DownloadOption) else None
        options = normalize_options(options)
        request = dict(freeze_download_song(song), _download_options=options, _download_choice=choice)
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
        from .online.download_probe import DownloadTransportError

        cb.get("on_stage", lambda k, stage: None)(key, "正在检查可用音频")
        offers, expected_priority = self._discover_offers(
            song, self._closed.is_set, options=options, choice=song.get("_download_choice"))
        best = None
        rejected_format = False
        choice = song.get("_download_choice")
        strict_choice = choice or options.get("verified_quality") or options.get("format")
        try:
            transfers = 0
            for offer in offers:
                if self._closed.is_set():
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
                    with self._lock:
                        self._tasks[key]["status"] = "downloading"
                    try:
                        part, info = self._fetch_audio(track, result, key, cb, self.download_dir)
                    except DownloadTransportError as exc:
                        if exc.status not in _REFRESH_HTTP or self._closed.is_set():
                            raise
                        result = self._resolve_rendition(track, option.native, 1.2)
                        if not result:
                            raise
                        part, info = self._fetch_audio(track, result, key, cb, self.download_dir)
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
                            self._cleanup(best[0])
                        best = part, track, info, offer
                        part = None
                    if choice or options.get("verified_quality"):
                        break
                except Exception as exc:
                    log.warning("download candidate rejected type=%s", type(exc).__name__)
                finally:
                    if part:
                        self._cleanup(part)
            if self._closed.is_set():
                self._fail(key, cb, "下载已取消")
                return
            if best is None:
                reason = "所选音质或格式暂不可用" if rejected_format or strict_choice else (
                    "来源暂未提供可下载音频" if not offers else "音频未能完整下载")
                self._fail(key, cb, reason)
                return
            part, track, info, selected = best
            base = self._filename_base(song.get("title"), song.get("artist"))
            final = os.path.join(self.download_dir, base + info["extension"])
            lowered = (not choice and options["quality"] == "best" and
                       (selected.priority < expected_priority or
                        not self._meets_declared_floor(expected_priority, info)))
            details = dict(quality=info["quality"], lyrics="not_requested", cover="not_requested",
                           metadata="not_requested", fallback=lowered,
                           requested_quality=options["quality"], provider_tier=selected.option.native,
                           declared_quality=selected.option.declared_quality,
                           availability_evidence=selected.evidence,
                           media_evidence=info.get("evidence"), codec=info.get("codec"),
                           bitrate=info.get("bitrate"), sample_rate=info.get("sample_rate"),
                           bit_depth=info.get("bits_per_sample"), channels=info.get("channels"))
            cb.get("on_stage", lambda k, stage: None)(key, "正在保存")
            lyric = cover = None
            for name, reader in (("lyrics", lambda: assets.lyric_for_track(self.online_manager, track)),
                                 ("cover", lambda: assets.cover_for_track(track, cancelled=self._closed.is_set))):
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
                            **({"cancelled": cancelled} if cancelled is not None else {}),
                            **({"expected_size": result["_expected_size"]} if result.get("_expected_size") else {})) <= 0:
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

        try:
            future = self._probe_pool.submit(probe, freeze_download_song(song))
        except (RuntimeError, TypeError, ValueError):
            self._probe_slots.release()
            return False
        future.add_done_callback(lambda _: self._probe_slots.release())
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

    def _discover_offers(self, song, cancelled, *, menu=False, options=None, choice=None):
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
        return inspect_audio(path, cancelled=self._closed.is_set)

    def _stream(self, url, headers, part, key, cb, *, cancelled=None, expected_size=None):
        from .online.download_probe import stream_download
        return stream_download(url, headers, part,
                               lambda progress: cb.get("on_progress", lambda k, p: None)(key, progress),
                               cancelled=lambda: self._closed.is_set() or (cancelled and cancelled()),
                               expected_size=expected_size)

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
        self._probe_pool.shutdown(wait=False, cancel_futures=True)
        self._pool.shutdown(wait=False, cancel_futures=True)
