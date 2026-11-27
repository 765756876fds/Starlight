"""Playback rendition negotiation and verified media facts. Independent of downloads and Qt."""
import math
import os
import time
import mutagen

from .online.resolver import _pick_play_headers

PREFERENCES = {"auto": "自动最高", "hires": "Hi-Res", "lossless": "无损", "high": "高品", "standard": "标准"}
PROTOCOLS = {"musicfree": ("super", "high", "standard", "low"), "lx": ("flac24bit", "flac", "320k", "128k")}


def rendition_order(preference, protocol):
    levels = PROTOCOLS.get(protocol, ())
    if not levels:
        return ()
    start = {"standard": 2 if protocol == "musicfree" else 3,
             "high": 1 if protocol == "musicfree" else 2,
             "lossless": 0 if protocol == "musicfree" else 1}.get(preference, 0)
    return levels[start:]


def _positive(value):
    return int(value) if type(value) in (int, float) and math.isfinite(value) and value > 0 else None


def media_facts(track, output, local_file=None, measured_bitrate=None):
    track, output = track or {}, output or {}
    codec = track.get("codec")
    facts = dict(codec=codec.upper() if isinstance(codec, str) and codec else None,
                 sample_rate=_positive(track.get("demux-samplerate")),
                 channels=_positive(track.get("demux-channel-count")), bits=None,
                 bitrate=_positive(track.get("demux-bitrate")) or _positive(measured_bitrate),
                 output_rate=_positive(output.get("samplerate")),
                 output_format=output.get("format") or None,
                 output_channels=_positive(output.get("channel-count")))
    if local_file and codec:
        try:
            media = mutagen.File(local_file)
            info = getattr(media, "info", None)
            if codec.lower() in {"flac", "alac", "wavpack"} or codec.lower().startswith("pcm_"):
                facts["bits"] = _positive(getattr(info, "bits_per_sample", None))
            facts["bitrate"] = _positive(getattr(info, "bitrate", None)) or facts["bitrate"]
        except (OSError, ValueError, mutagen.MutagenError):
            pass
    return facts


def resolve_playback(manager, track, preference, *, legacy_quality=None):
    resolver = getattr(type(manager), "get_playback_url", None)
    if resolver:
        return resolver(manager, track, preference)
    if legacy_quality:
        return manager.get_play_url(track, quality=legacy_quality)
    return manager.get_play_url(track)


def _same_media(left, right):
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    if left == right:
        return True
    if "://" in left or "://" in right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


class PlaybackQuality:
    """Holds the rendition for the current recording. Workers use the controller's existing epoch."""
    def __init__(self, controller):
        self.owner = controller
        self.preference = "auto"
        self.serial = 0
        self.song = None
        self.track = None
        self.result = {}
        self.alternatives = ()
        self.switching = False
        self.revision = 0

    def bind(self, song, track, result):
        self.song, self.track, self.result = song, track, dict(result)
        self.alternatives = ()
        self.revision += 1

    def path_for(self, song):
        if song is self.song and self.result.get("url"):
            return self.result["url"]
        return (song or {}).get("path")

    def inspect(self):
        owner = self.owner
        song, revision = owner.get_current_song(), self.revision
        if not song:
            return None
        path = self.path_for(song)
        read = owner.mpv.get_property
        if not _same_media(read("path", timeout=.18), path):
            return None
        track = read("current-tracks/audio", timeout=.18)
        if not isinstance(track, dict) or not track.get("codec"):
            return None
        output = read("audio-out-params", timeout=.18)
        bitrate = read("audio-bitrate", timeout=.18)
        local = path if song.get("source") != "online" and "://" not in path else None
        facts = media_facts(track, output, local, bitrate)
        if (song is not owner.get_current_song() or revision != self.revision
                or not _same_media(read("path", timeout=.18), path)):
            return None
        result = self.result if song is self.song else {}
        return dict(facts, requested=result.get("requested_quality"), declared=result.get("source_quality"),
                    requested_preference=result.get("requested_preference"), downgraded=bool(result.get("downgraded")))

    def reserve(self, preference):
        if preference not in PREFERENCES:
            raise ValueError("invalid quality preference")
        self.preference = preference
        self.serial += 1
        return self.serial, self.owner._play_request_id, self.owner.get_current_song(), preference

    def change(self, request, still_current):
        serial, epoch, song, preference = request
        owner = self.owner
        def same_recording():
            return (owner._running and not owner._user_stopped and song is owner.get_current_song()
                    and epoch == owner._play_request_id and owner._movie_permits_request())
        def current():
            return serial == self.serial and still_current() and same_recording()
        if not song or song is not self.song or not self.track:
            return "偏好已保存"
        if not current():
            return ""
        if self.result.get("supports_renditions") is False and not self.alternatives:
            return "该音源提供一个版本，偏好已保存"
        best = None
        start = time.monotonic()
        candidates = [self.track, *self.alternatives]
        for index, candidate in enumerate(candidates):
            if index and time.monotonic() - start > 2:
                break
            if not current():
                return ""
            try:
                result = resolve_playback(owner.online_manager, candidate, preference)
            except (OSError, ValueError, RuntimeError, TimeoutError):
                continue
            if result and isinstance(result.get("url"), str) and result["url"]:
                if result.get("direct_url"):
                    continue
                if best is None or not result.get("downgraded"):
                    best = candidate, result
                if not result.get("downgraded"):
                    break
        if not current():
            return ""
        if not best:
            return "该音质暂不可用，原版本继续播放"
        candidate, replacement = best
        if replacement.get("checked_transport"):
            with owner._lock:
                if not current():
                    return ""
                try:
                    replacement = owner._prepare_checked_playback(replacement)
                except (OSError, ValueError):
                    return "该音质暂不可用，原版本继续播放"
        original = self.result
        if _same_media(original.get("url"), replacement["url"]):
            with owner._lock:
                if not current():
                    return ""
                self.result = dict(replacement)
                self.revision += 1
            return "当前已是可用版本"
        read = owner.mpv.get_property
        if not _same_media(read("path", timeout=.2), original.get("url")):
            return "播放状态已变化，请重试"
        with owner._lock:
            if not current() or owner._is_switching_track:
                return ""
            position = read("time-pos", timeout=.16)
            paused = read("pause", timeout=.16)
            if type(position) not in (int,float) or type(paused) is not bool:
                return "进度暂不可读取，请重试"
            if not current():
                return ""
            self.switching = True
            try:
                receipt = self._load(replacement, position, paused)
            except Exception:
                self.switching = False
                raise
        try:
            if self._confirmed(replacement["url"], receipt, same_recording):
                with owner._lock:
                    if not same_recording():
                        return ""
                    if candidate is not self.track:
                        previous = self.track
                        self.track = candidate
                        self.alternatives = tuple(t for t in (previous, *self.alternatives)
                                                  if t is not candidate)
                    if replacement.get("_checked_address"):
                        owner._checked_playback.retain(replacement["url"])
                    self.result = dict(replacement)
                    self.revision += 1
                return "已切换到可用音质" if current() else ""
            if not same_recording():
                return ""
            # A timeout may follow an accepted load. Check the original decoder before rollback.
            if not self._confirmed(original["url"], None, same_recording, seconds=.12):
                with owner._lock:
                    if not same_recording():
                        return ""
                    paused = getattr(owner, "_paused_snapshot", paused)
                    restored = self._load(original, position, paused)
                if not self._confirmed(original["url"], restored, same_recording):
                    return "暂时无法恢复播放，请重试"
            return "该音质暂不可用，已保留原版本"
        finally:
            self.switching = False

    def _load(self, result, position, paused):
        headers = _pick_play_headers(result.get("headers"))
        return self.owner.mpv.load_rendition(result["url"], position, paused, headers)

    def _confirmed(self, path, receipt, valid, *, seconds=3.0):
        if isinstance(receipt,dict) and receipt.get("error") != "success":
            return False
        read = self.owner.mpv.get_property
        deadline = time.monotonic() + seconds
        while valid() and time.monotonic() < deadline:
            if _same_media(read("path", timeout=.12),path):
                track = read("current-tracks/audio",timeout=.12)
                pos = read("time-pos",timeout=.12)
                if isinstance(track,dict) and track.get("codec") and type(pos) in (int,float):
                    return valid() and _same_media(read("path",timeout=.12),path)
            time.sleep(.035)
        return False
