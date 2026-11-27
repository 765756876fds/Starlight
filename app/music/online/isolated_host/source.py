"""Explicitly approved MusicFree/LX sources behind the AppContainer broker."""

import math
from threading import Lock

from ...download_journal import _raw, plain
from ...lyrics import plugin_lyric_text
from ...playback_quality import PROTOCOLS, rendition_order
from ..models import OnlineTrack, SearchPage
from ..source import MusicSource, SourceSearchError
from ..source_authority import DownloadTransportError, parse_remote_url

_LX_TIERS = {"flac24bit": "hires", "flac": "lossless", "320k": "high", "128k": "standard"}
_SEARCH_FAILURES = {"timeout", "unavailable", "structure", "busy", "interrupted"}
_HOST_FAILURES = {
    "isolated_source_unavailable": "unavailable",
    "isolated_host_timeout": "timeout",
    "isolated_source_revoked": "interrupted",
    "isolated_host_busy": "busy",
    "isolated_source_busy": "busy",
    "isolated_cleanup_pending": "busy",
    "too_many_pending": "busy",
}


def _search_failure(code):
    code = str(code).lower()
    if code.startswith("structure_") or code == "protocol_oversized":
        return "structure"
    return _HOST_FAILURES.get(code, code if code in _SEARCH_FAILURES else "unknown")


def _remote_result(result):
    if not isinstance(result, dict):
        return None
    try:
        parse_remote_url(result.get("url"))
    except DownloadTransportError:
        return None
    # Destination and headers are authorized at the OnlineSourceManager boundary.
    return {key: result[key] for key in ("url", "headers", "quality", "source_quality",
            "requested_quality", "supports_renditions", "direct_url", "downgraded") if key in result}


class IsolatedMusicSource(MusicSource):
    def __init__(self, name, plugin_type, providers, invoke, *, close=None, download_qualities=None):
        if plugin_type not in PROTOCOLS or not name or not callable(invoke):
            raise ValueError("Unsupported isolated music source")
        self.name = name
        self.plugin_type = plugin_type
        self.providers = frozenset(providers)
        self._invoke = invoke
        self._close = close
        self._configured_qualities = None if download_qualities is None else tuple(download_qualities)
        self._lifecycle = Lock()
        self._token = object()

    def search_snapshot(self):
        """Pure catalog lookup. Registration need not launch a plugin on the GUI thread."""
        with self._lifecycle:
            token = self._token
        return token, (self.name,) if token is not None and self.plugin_type == "musicfree" else ()

    def has_search_process(self, token):
        with self._lifecycle:
            return token is not None and token is self._token

    def search_page(self, token, source, keyword, page):
        if (source != self.name or self.plugin_type != "musicfree" or not self.has_search_process(token)):
            raise SourceSearchError("unavailable")
        if type(page) is not int or not 1 <= page <= 2**53 - 1:
            raise SourceSearchError("structure")
        try:
            result = self._invoke("searchPage", {"source": source, "keyword": keyword, "page": page}, timeout=9.0)
        except TimeoutError as error:
            raise SourceSearchError("timeout") from error
        except (OSError, RuntimeError) as error:
            raise SourceSearchError(_search_failure(error)) from error
        if not self.has_search_process(token):
            raise SourceSearchError("interrupted")
        if not isinstance(result, dict):
            raise SourceSearchError("structure")
        if result.get("status") != "ok":
            raise SourceSearchError(_search_failure(result.get("status")))
        rows, end, total = result.get("tracks"), result.get("is_end"), result.get("total")
        if (result.get("source") != source or type(result.get("page")) is not int or result["page"] != page
                or not isinstance(rows, list) or len(rows) > 120 or (end is not None and type(end) is not bool)
                or (total is not None and (type(total) is not int or total < 0))):
            raise SourceSearchError("structure")
        tracks = [track for row in rows if (track := self._track(row)) is not None]
        if rows and not tracks:
            raise SourceSearchError("structure")
        return SearchPage(tracks, end, total)

    def search(self, keyword, limit=10):
        token, names = self.search_snapshot()
        return self.search_page(token, names[0], keyword, 1).tracks[:limit] if names else []

    def _track(self, row):
        if not isinstance(row, dict) or row.get("source_name") != self.name:
            return None
        track_id = plain(row.get("track_id"))
        if not track_id:
            return None
        duration = row.get("duration_seconds")
        if type(duration) not in (int, float) or not 0 < duration <= 2**53 - 1:
            duration = 0
        extra = row.get("extra")
        raw = _raw(extra.get("_raw")) if isinstance(extra, dict) else {}
        return OnlineTrack(self.name, track_id, plain(row.get("title")), plain(row.get("artist")),
                           plain(row.get("album")), duration, extra={"_raw": raw})

    def _track_params(self, track, **params):
        if track.source_name != self.name or not plain(track.track_id):
            return None
        raw = _raw(track.extra.get("_raw")) if isinstance(track.extra, dict) else {}
        if self.plugin_type == "lx":
            provider = raw.get("musicInfo", {}).get("source") or raw.get("source")
            if not isinstance(provider, str) or provider not in self.providers:
                return None
        return {"source": self.name, "track_id": track.track_id, "raw_item": raw or None, **params}

    def _request(self, method, params, *, timeout=7.0):
        token, _ = self.search_snapshot()
        if token is None or params is None:
            return None
        try:
            result = self._invoke(method, params, timeout=timeout)
        except (OSError, ValueError, RuntimeError):
            return None
        return result if self.has_search_process(token) else None

    def download_tiers(self, track):
        params = self._track_params(track)
        if params is None:
            return ()
        response = self._request("ping", {}, timeout=2.0)
        capabilities = response.get("capabilities") if isinstance(response, dict) else None
        if not isinstance(capabilities, dict):
            return ()
        if self.plugin_type == "musicfree" and capabilities.get("resolve_media") is False:
            return ((None, None),) if capabilities.get("direct_url") is True else ()
        if self.plugin_type == "lx":
            raw = params["raw_item"]
            provider = raw.get("musicInfo", {}).get("source") or raw.get("source")
            by_provider = capabilities.get("provider_qualities")
            supported = by_provider.get(provider, ()) if isinstance(by_provider, dict) else ()
        else:
            supported = PROTOCOLS[self.plugin_type] if capabilities.get("resolve_media") is True else ()
        if not isinstance(supported, (list, tuple)):
            return ()
        keys = PROTOCOLS[self.plugin_type] if self._configured_qualities is None else self._configured_qualities
        return tuple((_LX_TIERS.get(key, key), key) for key in PROTOCOLS[self.plugin_type]
                     if key in keys and key in supported)

    def get_download_rendition(self, track, native_tier, *, timeout=2.0):
        if (type(timeout) not in (int, float) or not 0 < timeout <= 15 or not math.isfinite(timeout)
                or (native_tier is not None and native_tier not in PROTOCOLS[self.plugin_type])
                or (self._configured_qualities is not None and native_tier is not None
                    and native_tier not in self._configured_qualities)):
            return None
        params = self._track_params(track, quality=native_tier)
        return _remote_result(self._request("downloadUrl", params, timeout=timeout))

    def get_playback_rendition(self, track, preference):
        order = rendition_order(preference, self.plugin_type)
        if self._configured_qualities is not None:
            order = [tier for tier in order if tier in self._configured_qualities]
        params = self._track_params(track, playback_qualities=list(order))
        return _remote_result(self._request("getUrl", params))

    def get_play_url(self, track, quality=None):
        params = self._track_params(track, quality=quality)
        return _remote_result(self._request("getUrl", params))

    def get_cover_url(self, track):
        return _remote_result(self._request("getCover", self._track_params(track)))

    def get_lyric(self, track):
        result = self._request("getLyric", self._track_params(track))
        if not isinstance(result, dict):
            return None
        return plugin_lyric_text(result.get("lyric"), result.get("lxlyric")) or _remote_result(result)

    def close(self):
        with self._lifecycle:
            if self._token is None:
                return
            self._token = None
        if self._close is not None:
            self._close()
