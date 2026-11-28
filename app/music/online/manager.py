"""OnlineSourceManager：注册/搜索/播放/歌词。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import partial
from dataclasses import replace

from app.core.logger import get_logger

from .cache import OnlineCache
from .models import OnlineTrack, SearchPage, SearchResults, SearchTracks
from .paged_search import PagedSearch, SearchTarget
from .source import MusicSource, SourceSearchError
from .source_authority import DownloadTransportError, SourceLease, authorization_plugin

logger = get_logger()


class OnlineSourceManager:
    PAGED_WORKERS = 12

    def __init__(self, cache_ttl: float = 300.0, max_workers: int = 4,
                 request_timeout: float = 5.0, *, allow_legacy_sources: bool = True, catalog_routes=()):
        if catalog_routes and allow_legacy_sources:
            raise ValueError("catalog routes require a managed registry")
        self._catalog_routes = {route.catalog: route for route in catalog_routes}
        self._sources: dict[str, MusicSource] = {}
        self._source_leases: dict[str, SourceLease] = {}
        self._managed_names: set[str] = set()
        self._cache = OnlineCache(ttl_seconds=cache_ttl)
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._timeout = request_timeout
        self._errors: dict[str, str] = {}
        # 各源最近一次搜索耗时（秒）——同音质选源时用"最快"排序。
        self._latency: dict[str, float] = {}
        # 音源版本号：register 递增、unregister 单调递增（删除标记）。
        # 同名源被替换/注销后，在途旧搜索任务的结果必须被丢弃且不得写回缓存。
        self._source_versions: dict[str, int] = {}
        # 生命周期锁：register/unregister 的版本递增+缓存删除，与 search_all 的
        # 版本校验+缓存写入必须互斥，保证"版本检查→cache.set"之间无法被
        # unregister/同名 register 插入（消除旧结果回写污染窗口）。
        # 锁内只做 dict/缓存操作（微秒级），不等待搜索线程，无死锁。
        self._life_lock = threading.Lock()
        self._paged_executor = ThreadPoolExecutor(max_workers=self.PAGED_WORKERS,
                                                  thread_name_prefix="music-search")
        self._paged_active = {}
        self._legacy_download_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="legacy-download-url")
        self._legacy_download_active = {}
        self._paged_peak = 0
        self._closed = False
        self.allow_legacy_sources = allow_legacy_sources
        self.isolated_source_overview = None

    def register(self, source: MusicSource, *, lease: SourceLease | None = None) -> None:
        with self._life_lock:
            if self._closed:
                raise RuntimeError("music source manager is closed")
            if lease is not None and (not isinstance(lease, SourceLease)
                                     or lease.source_id != source.name or not lease.active()):
                raise DownloadTransportError("source_scope")
            if lease is None and (not self.allow_legacy_sources or source.name in self._managed_names):
                raise DownloadTransportError("source_scope")
            previous = self._sources.get(source.name)
            previous_lease = self._source_leases.get(source.name)
            if lease is not None and lease is previous_lease and previous is not source:
                raise DownloadTransportError("source_scope")
            if lease is not None:
                self._managed_names.add(source.name)
                self._source_leases[source.name] = lease
            if previous_lease is not None and previous_lease is not lease:
                previous_lease.retire()
            if previous is not None:
                self._cache.delete(source.name)
            self._sources[source.name] = source
            self._source_versions[source.name] = self._source_versions.get(source.name, 0) + 1
        if previous is not None and previous is not source:
            try:
                previous.close()
            except Exception:
                logger.exception("Failed to close a replaced music source: %s", source.name)

    def load_custom_sources(self, directory: str) -> int:
        """扫描目录下 *.py，每个文件必须定义 create_source() -> MusicSource。"""
        if not self.allow_legacy_sources:
            return 0
        import importlib.util
        import os
        loaded = 0
        if not directory or not os.path.isdir(directory):
            return 0
        for fname in sorted(os.listdir(directory)):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(directory, fname)
            try:
                spec = importlib.util.spec_from_file_location(f"custom_{fname}", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                if not hasattr(mod, "create_source"):
                    print(f"[OnlineMusic] {fname}: 无 create_source()，跳过")
                    continue
                src = mod.create_source()
                self.register(src)
                loaded += 1
                print(f"[OnlineMusic] 已加载自定义源: {src.name}")
            except Exception as e:
                print(f"[OnlineMusic] {fname} 加载失败: {e}")
        return loaded

    def unregister(self, name: str) -> None:
        with self._life_lock:
            s = self._sources.pop(name, None)
            lease = self._source_leases.pop(name, None)
            if lease is not None:
                lease.retire()
            # 版本单调递增并保留（删除标记）：同名源重新注册后版本必然不同，
            # 在途旧搜索任务的结果会被版本校验丢弃，不会写回缓存。
            self._source_versions[name] = self._source_versions.get(name, 0) + 1
            # 清理该源全部缓存，避免同名替换后命中 stale 数据。
            # 与 search_all 的版本校验+缓存写入同锁互斥：若替换发生在
            # "检查通过→set"之间，delete 必然在 set 之后执行，旧结果被清除。
            self._cache.delete(name)
        if s:
            s.close()

    def list_sources(self) -> list[str]:
        return list(self._sources.keys())

    def renew_source_lease(self, source, previous, new):
        """Replace only the still-registered instance's retired authorization."""
        with self._life_lock:
            if (self._closed or self._sources.get(source.name) is not source
                    or self._source_leases.get(source.name) is not previous
                    or previous.active() or not new.active()
                    or (new.source_id, new.plugin_id) != (previous.source_id, previous.plugin_id)):
                return False
            self._source_leases[source.name] = new
            self._source_versions[source.name] += 1
            self._cache.delete(source.name)
            return True

    def get_source(self, name: str) -> MusicSource | None:
        """按注册名取源（UI 等外部组件用，避免摸私有 dict）。"""
        return self._sources.get(name)

    def invalidate(self, name: str) -> None:
        """清除某源的搜索缓存（桥接/插件重载后调用，避免旧结果掩盖新注册表）。

        推进代次但不注销源；失效前启动的搜索不得重新写回缓存。
        """
        with self._life_lock:
            self._source_versions[name] = self._source_versions.get(name, 0) + 1
            self._cache.delete(name)

    def _managed_tracks(self, source, result):
        with self._life_lock:
            lease = self._source_leases.get(source.name)
            if lease is None:
                if source.name in self._managed_names:
                    raise SourceSearchError("unavailable")
                return result
            if not lease.active() or self._sources.get(source.name) is not source:
                raise SourceSearchError("unavailable")
        page = isinstance(result, SearchPage)
        tracks = result.tracks if page else result
        if not isinstance(tracks, list):
            raise SourceSearchError("structure")
        from ..download_journal import _raw
        clean = []
        for track in tracks:
            if not isinstance(track, OnlineTrack) or lease.source_id != track.source_name:
                raise SourceSearchError("structure")
            raw = track.extra.get("_raw", {}) if isinstance(track.extra, dict) else {}
            clean.append(replace(track, cover_url=None, extra={"_raw": _raw(raw)},
                                 authorization_plugin=lease.plugin_id))
        if page:
            return replace(result, tracks=clean)
        return SearchTracks(clean, errors=getattr(result, "errors", None),
                            successful=getattr(result, "successful", 1))

    def open_search(self, keyword: str) -> PagedSearch:
        targets = []
        with self._life_lock:
            sources = [(name, src, self._source_versions[name])
                       for name, src in self._sources.items()
                       if name not in self._source_leases or self._source_leases[name].active()]
        for name, src, version in sources:
            if not hasattr(src, "search_snapshot"):
                targets.append(SearchTarget(name, name, src, version, False))
                continue
            process, aliases = src.search_snapshot()
            for alias in aliases:
                if name in self._managed_names and alias != name:
                    continue
                targets.append(SearchTarget(name, alias, src, version, True, process))
        return PagedSearch(self, keyword, targets)

    def _target_is_current(self, target: SearchTarget) -> bool:
        return (not self._closed and self._source_versions.get(target.owner) == target.version
                and (target.owner not in self._source_leases or self._source_leases[target.owner].active())
                and (not target.pageable or target.source.has_search_process(target.process)))

    def submit_search_page(self, target, keyword, page):
        # Keep counting a job until the Python call actually returns. The
        # session can stop waiting without creating unbounded executor work.
        with self._life_lock:
            if self._closed or target.key in self._paged_active or len(self._paged_active) >= self.PAGED_WORKERS:
                return None
            read = (partial(target.source.search_page, target.process, target.name, keyword, page)
                    if target.pageable else partial(target.source.search, keyword, 30))
            future = self._paged_executor.submit(self._read_search_page, target, read)
            self._paged_active[target.key] = future
            self._paged_peak = max(self._paged_peak, len(self._paged_active))

        def released(_):
            with self._life_lock:
                if self._paged_active.get(target.key) is future:
                    self._paged_active.pop(target.key)

        future.add_done_callback(released)
        return future

    def _read_search_page(self, target, read):
        started = time.monotonic()
        try:
            return self._managed_tracks(target.source, read())
        finally:
            with self._life_lock:
                if self._target_is_current(target):
                    self._latency[target.name] = time.monotonic() - started

    def paged_search_activity(self):
        with self._life_lock:
            return len(self._paged_active), self._paged_peak

    def search_all(self, keyword: str, limit_per_source: int = 10
                   ) -> dict[str, list[OnlineTrack]]:
        out = SearchResults()
        futs = {}
        with self._life_lock:
            sources = []
            for name, src in self._sources.items():
                if name in self._source_leases and not self._source_leases[name].active():
                    out.errors[name] = "unavailable"
                    continue
                cached = self._cache.get(name, keyword, limit_per_source)
                if cached is not None:
                    out[name] = cached
                    out.successful_sources.add(name)
                else:
                    sources.append((name, src, self._source_versions.get(name, 0)))
        for name, src, ver in sources:
            fut = self._executor.submit(self._safe_search, src, keyword,
                                        limit_per_source, ver)
            futs[fut] = (name, ver)
        try:
            for fut in as_completed(futs, timeout=self._timeout + 2):
                name, ver = futs[fut]
                try:
                    tracks = fut.result(timeout=self._timeout)
                    # 版本校验与缓存写入作为原子临界区：期间 unregister/同名
                    # register 无法插入（生命周期锁），彻底关闭"检查通过→set
                    # 之前被替换导致旧结果回写污染"的竞争窗口。
                    with self._life_lock:
                        if (self._source_versions.get(name) != ver or
                                name in self._source_leases and not self._source_leases[name].active()):
                            out.errors[name] = "interrupted"
                            continue
                        out[name] = tracks
                        errors = getattr(tracks, "errors", {})
                        if getattr(tracks, "successful", 1) > 0:
                            out.successful_sources.add(name)
                        if errors:
                            out.errors[name] = next(iter(errors.values()))
                            self._errors[name] = str(out.errors[name])
                        else:
                            self._errors.pop(name, None)
                            self._cache.set(name, keyword, limit_per_source, tracks)
                except Exception as e:
                    with self._life_lock:
                        if self._source_versions.get(name) != ver:
                            out.errors[name] = "interrupted"
                            continue
                        self._errors[name] = str(e)
                        out.errors[name] = getattr(
                            e, "category", "timeout" if isinstance(e, TimeoutError) else "unknown")
                        out[name] = []
        except TimeoutError:
            for fut, (name, ver) in futs.items():
                if name not in out:
                    with self._life_lock:
                        if self._source_versions.get(name) != ver:
                            out.errors[name] = "interrupted"
                            continue
                        out[name] = []
                        out.errors[name] = "timeout"
                        self._errors[name] = "timeout"
        return out

    def _safe_search(self, src: MusicSource, kw: str, limit: int,
                     version: int) -> list[OnlineTrack]:
        t0 = time.monotonic()
        try:
            result = src.search(kw, limit)
            if not isinstance(result, list):
                raise SourceSearchError("unknown" if result is None else "structure")
            return self._managed_tracks(src, result)
        finally:
            self._latency[src.name] = time.monotonic() - t0

    def get_latency(self, name: str) -> float | None:
        """某源最近一次搜索耗时（秒）；无记录返回 None（调用方用默认值兜底）。"""
        return self._latency.get(name)

    @staticmethod
    def _call_get_url(src, track, quality):
        """源取播放 URL；不支持 quality 参数的源（demo/http/archive）TypeError 兜底。"""
        if quality is None:
            return src.get_play_url(track)
        try:
            return src.get_play_url(track, quality=quality)
        except TypeError:
            return src.get_play_url(track)

    def _managed(self, track):
        extra = getattr(track, "extra", None)
        return bool(authorization_plugin(track) or track.source_name in self._managed_names
                    or isinstance(extra, dict) and "_catalog" in extra)

    def _managed_owner(self, track):
        return self._managed_context(track)[:2]

    def _managed_context(self, track):
        from .catalog_routes import routed_native_id, stored_route

        with self._life_lock:
            owner, lease = self._sources.get(track.source_name), self._source_leases.get(track.source_name)
            declared = authorization_plugin(track)
            if (self._closed or owner is None or lease is None or not lease.active()
                    or lease.plugin_id != declared):
                return None, None, None
            extra = track.extra if isinstance(track.extra, dict) else {}
            if "_catalog" not in extra:
                return owner, lease, None
            try:
                ref = stored_route(extra["_catalog"])
            except ValueError:
                return None, None, None
            route = self._catalog_routes.get(ref["source"])
            catalog = self._source_leases.get(ref["source"])
            raw = extra.get("_raw")
            info = raw.get("musicInfo") if isinstance(raw, dict) else None
            if (route is None or catalog is None or not catalog.active() or ref["plugin"] != catalog.plugin_id
                    or catalog._authority is not lease._authority or route.platform != ref["platform"]
                    or track.source_name not in route.resolvers or getattr(owner, "plugin_type", None) != "lx"
                    or route.platform not in owner.providers or not isinstance(info, dict)
                    or routed_native_id(track.track_id, info, route.platform, route.catalog) is None):
                return None, None, None
            return owner, lease, (catalog, ref["track"])

    def catalog_candidates(self, track):
        """Expand resource choices only when processing an explicit recording."""
        from .catalog_routes import resolver_track

        route = self._catalog_routes.get(track.source_name)
        if route is None:
            return ()
        owner, _ = self._managed_owner(track)
        if getattr(owner, "plugin_type", None) != "musicfree":
            return ()
        candidates = (resolver_track(track, route.platform, name) for name in route.resolvers)
        return tuple(item for item in candidates if item is not None and self._managed_owner(item)[0] is not None)

    def _resolution_choices(self, track):
        if track.source_name not in self._catalog_routes:
            yield track
            return
        original = self._managed_context(track)
        for candidate in (track, *self.catalog_candidates(track)):
            if original != self._managed_context(track):
                return
            yield candidate

    def _attachment_choices(self, track):
        from .catalog_routes import original_catalog_track

        original = self._managed_context(track)
        yield from self._resolution_choices(track)
        if original[2] is not None and self._managed_context(track) == original:
            yield original_catalog_track(track, track.extra["_catalog"])

    def _authorized_result(self, track, owner, lease, result, kind, catalog=None):
        if self._managed_context(track) != (owner, lease, catalog) or result is None:
            return None
        result = self._normalize_play_result(result, owner)
        if not result or not result.get("url"):
            return None
        try:
            grant = lease.resource(kind, result["url"], result.get("headers"),
                                   resource_id=str(track.track_id))
            if catalog is not None:
                grant = grant.for_catalog(*catalog)
        except DownloadTransportError:
            return None
        # Do not accept checked-address flags or provenance from a plugin.
        fields = ("quality", "source_quality", "requested_quality", "supports_renditions", "direct_url", "downgraded")
        clean = {name: result[name] for name in fields if type(result.get(name)) in (str, bool)}
        return {**clean, "url": grant.url, "headers": None, "_authorization": grant,
                "checked_transport": kind == "playback"}

    def _legacy_routes(self, track):
        with self._life_lock:
            if self._closed or self._managed(track):
                return None, ()
            return self._sources.get(track.source_name), tuple(
                (name, src) for name, src in self._sources.items() if name not in self._managed_names)

    def _legacy_current(self, track, source):
        with self._life_lock:
            return (not self._closed and not self._managed(track) and source.name not in self._managed_names
                    and self._sources.get(source.name) is source)

    def get_cover_resource(self, track):
        for candidate in self._attachment_choices(track):
            owner, lease, catalog = self._managed_context(candidate)
            getter = getattr(owner, "get_cover_url", None)
            if not callable(getter):
                continue
            try:
                resolved = self._authorized_result(candidate, owner, lease, getter(candidate), "cover", catalog)
                if resolved is not None:
                    return resolved
            except (OSError, ValueError, TimeoutError, RuntimeError):
                continue
        return None

    def is_managed_track(self, track):
        return self._managed(track)

    def _download_owner(self, track: OnlineTrack):
        if not track.source_name or not track.track_id:
            return None
        if self._managed(track):
            return self._managed_owner(track)[0]
        with self._life_lock:
            if self._closed:
                return None
            source = self._sources.get(track.source_name)
            if source is not None:
                return source
            candidates = tuple(item for name, item in self._sources.items() if name not in self._managed_names)
        # Only a source with an explicit exact-rendition adapter can claim a
        # nested provider ID. Ambiguous claims are rejected.
        owners = [item for item in candidates
                  if callable(getattr(item, "get_download_rendition", None))
                  and callable(getattr(item, "accepts", None)) and item.accepts(track)]
        return owners[0] if len(owners) == 1 else None

    def download_tiers(self, track: OnlineTrack) -> tuple:
        """Canonical quality tiers and the exact keys the same owner accepts."""
        source = self._download_owner(track)
        if source is None:
            return ()
        adapter = getattr(source, "download_tiers", None)
        return adapter(track) if callable(adapter) else ((None, None),)

    def _legacy_download_url(self, source, track, timeout):
        key = track.source_name
        with self._life_lock:
            if self._closed or key in self._legacy_download_active or len(self._legacy_download_active) >= 3:
                return None
            future = self._legacy_download_executor.submit(source.get_play_url, track)
            self._legacy_download_active[key] = future

        def released(_):
            with self._life_lock:
                if self._legacy_download_active.get(key) is future:
                    self._legacy_download_active.pop(key)

        future.add_done_callback(released)
        return future.result(timeout=timeout)

    def get_download_rendition(self, track: OnlineTrack, native_tier, *, timeout=2.0):
        """Resolve one representation within this resource's owner only."""
        source = self._download_owner(track)
        if source is None:
            return None
        lease, catalog = None, None
        if self._managed(track):
            current, lease, catalog = self._managed_context(track)
            if current is not source:
                return None
        resolver = getattr(source, "get_download_rendition", None)
        try:
            if callable(resolver):
                result = resolver(track, native_tier, timeout=timeout)
            elif native_tier is None:
                result = self._legacy_download_url(source, track, timeout)
            else:
                return None
            if lease is not None:
                result = self._authorized_result(track, source, lease, result, "download", catalog)
            else:
                result = self._normalize_play_result(result, source)
            if not result or not isinstance(result.get("url"), str) or not result["url"].strip():
                return None
            # Do not surface the address if the owner or registration mode changed.
            if (self._download_owner(track) is not source
                    or lease is None and not self._legacy_current(track, source)):
                return None
            declared = result.get("source_quality")
            return dict(result, source=track.source_name, requested_quality=native_tier,
                        source_quality=declared if isinstance(declared, str) else None)
        except Exception as exc:
            logger.debug("download address resolution failed type=%s", type(exc).__name__)
            return None

    def get_playback_url(self, track: OnlineTrack, preference: str):
        """Try the owner and any approved same-platform catalog route in order."""
        best = None
        for candidate in self._resolution_choices(track):
            result = self._playback_rendition(candidate, preference)
            if result:
                if not result.get("downgraded"):
                    return result
                best = best or result
        grant = best.get("_authorization") if best else None
        return best if grant is None or grant.active() else None

    def _playback_rendition(self, track, preference):
        managed = self._managed(track)
        if managed:
            source, lease, catalog = self._managed_context(track)
        else:
            source, legacy = self._legacy_routes(track)
            if source is None:
                source = next((item for _, item in legacy
                               if callable(getattr(item, "accepts", None)) and item.accepts(track)), None)
        if source is None:
            return None
        resolver = getattr(source, "get_playback_rendition", None)
        try:
            result = resolver(track, preference) if callable(resolver) else source.get_play_url(track)
        except (OSError, ValueError, TimeoutError, RuntimeError):
            return None
        if managed:
            result = self._authorized_result(track, source, lease, result, "playback", catalog)
        elif self._legacy_current(track, source):
            result = self._normalize_play_result(result, source)
        else:
            return None
        if result and result.get("url"):
            return dict(result, requested_preference=preference,
                        supports_renditions=result.get("supports_renditions", callable(resolver)))
        return None

    def get_play_url(self, track: OnlineTrack, quality: str | None = None) -> dict | None:
        if self._managed(track):
            if quality is None and track.source_name in self._catalog_routes:
                return self.get_playback_url(track, "auto")
            owner, lease, catalog = self._managed_context(track)
            if owner is None:
                return None
            try:
                return self._authorized_result(track, owner, lease,
                                               self._call_get_url(owner, track, quality), "playback", catalog)
            except (OSError, ValueError, TimeoutError, RuntimeError):
                return None
        # 定向路由：track.source_name 通常等于注册名（自定义 HTTP 源）。
        tried = set()
        src, legacy = self._legacy_routes(track)
        if src is not None:
            tried.add(id(src))
            try:
                r = self._normalize_play_result(self._call_get_url(src, track, quality), src)
                if r and self._legacy_current(track, src):
                    return r
            except Exception as e:
                self._errors[track.source_name] = str(e)
                logger.warning(f"定向源 {track.source_name} 失败，回退其他源: {e}")
        # 定向 miss 或定向失败：遍历 accepts 命中的源（不重复定向源）
        for _, s in legacy:
            if id(s) in tried:
                continue
            try:
                acc = getattr(s, "accepts", None)
                if acc is None or not acc(track):
                    continue
                tried.add(id(s))
                r = self._normalize_play_result(self._call_get_url(s, track, quality), s)
                if r and self._legacy_current(track, s):
                    return r
            except Exception as e:
                logger.warning(f"获取在线源失败: {e}")
                continue
        # 仍 miss：回退全遍历（不重复已试源）
        for _, s in legacy:
            if id(s) in tried:
                continue
            try:
                r = self._normalize_play_result(self._call_get_url(s, track, quality), s)
                if r and self._legacy_current(track, s):
                    return r
            except Exception:
                continue
        return None

    @staticmethod
    def _normalize_play_result(r, source):
        """str 型源（http/demo/archive）包装成 dict；dict 型（js_bridge）原样。"""
        if not r:
            return None
        if isinstance(r, dict):
            if "kuwo_rid" in r or "playback_metadata" in r:
                from .sources.js_source import JSMusicSource
                if not isinstance(source, JSMusicSource):
                    return {k: v for k, v in r.items() if k not in {"kuwo_rid", "playback_metadata"}}
            return r
        return {"url": r, "headers": None}

    def get_lyric(self, track: OnlineTrack) -> str | None:
        if self._managed(track):
            for candidate in self._attachment_choices(track):
                result = self._managed_lyric(candidate)
                if result:
                    return result
            return None
        # 定向路由：track.source_name 通常等于注册名（自定义 HTTP 源）。
        src, legacy = self._legacy_routes(track)
        if src is not None:
            try:
                lyric = src.get_lyric(track)
                return lyric if self._legacy_current(track, src) else None
            except Exception:
                return None
        # 定向 miss：track.source_name 可能是 js_bridge 的内部插件名（如"酷狗音乐"），
        # 而非注册名（"js_bridge"）。对齐 get_play_url：accepts 命中源优先路由，
        # 避免遍历 http 源对陌生 track 发无谓请求。不加全遍历——歌词无强回退需求。
        for _, s in legacy:
            try:
                acc = getattr(s, "accepts", None)
                if acc is None or not acc(track):
                    continue
                r = s.get_lyric(track)
                if r and self._legacy_current(track, s):
                    return r
            except Exception:
                continue  # 单源失败继续下一个
        return None

    def _managed_lyric(self, track):
        owner, lease, catalog = self._managed_context(track)
        if owner is None:
            return None
        try:
            result = owner.get_lyric(track)
            if self._managed_context(track) != (owner, lease, catalog):
                return None
            if isinstance(result, str):
                return result
            bound = self._authorized_result(track, owner, lease, result, "lyrics", catalog)
            if bound is None:
                return None
            from .download_probe import read_download_attachment
            return read_download_attachment(bound["url"], authorization=bound["_authorization"],
                                            kind="lyrics", max_bytes=2_000_000).decode("utf-8")
        except (OSError, ValueError, TimeoutError, RuntimeError):
            return None

    def get_top_lists(self, source=None) -> dict[str, list]:
        """聚合各源榜单分组（源自报能力，无该方法/失败自动跳过）。

        返回 {源名或插件名: [分组]}——js_bridge 内部按插件名分组返回；
        自定义源若实现 get_top_lists 同样聚合。失败记录 _errors 不抛。
        """
        from .models import CatalogResults
        out = CatalogResults()
        with self._life_lock:
            if source in self._managed_names:
                return out
            sources = [(name, src) for name, src in self._sources.items() if name not in self._managed_names]
        for name, src in sources:
            if source and name not in (source, "js_bridge"):
                continue
            fn = getattr(src, "get_top_lists", None)
            if not callable(fn):
                continue
            try:
                r = fn(source=source) if source and name == "js_bridge" else fn()
                if r:
                    out.update(r)
                out.errors.update(getattr(r, "errors", {}))
            except Exception as e:
                self._errors[name] = str(e)
                out.errors[name] = getattr(e, "category", "unknown")
        return out

    def get_top_list_detail(self, source: str, list_id: str,
                            page: int = 1, *, limit=None) -> dict | None:
        """单榜单详情：路由到实现 get_top_list_detail 的源（js_bridge 按插件名）。

        返回 {is_end: bool, tracks: [OnlineTrack]}；无能力/失败 → None。
        """
        with self._life_lock:
            if source in self._managed_names:
                return None
            sources = [(name, src) for name, src in self._sources.items() if name not in self._managed_names]
        for name, src in sources:
            if limit is not None and name not in (source, "js_bridge"):
                continue
            fn = getattr(src, "get_top_list_detail", None)
            if not callable(fn):
                continue
            try:
                if limit is not None and name == "js_bridge":
                    r = fn(source, list_id, page, limit=limit)
                else:
                    r = fn(source, list_id, page)
                if r is not None:
                    return r
            except Exception as e:
                self._errors[name] = str(e)
        return None

    def discovery_sources(self):
        """Read bridge metadata, without making a network request in the GUI."""
        out = []
        with self._life_lock:
            sources = [src for name, src in self._sources.items() if name not in self._managed_names]
        for src in sources:
            getter = getattr(src, "list_plugins", None)
            if callable(getter):
                out.extend(p for p in getter() if p.get("status") == "ok" and p.get("capabilities"))
        return out

    def browse(self, source, operation, **params):
        with self._life_lock:
            if source in self._managed_names:
                raise SourceSearchError("unavailable")
            sources = [src for name, src in self._sources.items() if name not in self._managed_names]
        for src in sources:
            getter = getattr(src, "list_plugins", None)
            if callable(getter) and any(p.get("name") == source for p in getter()):
                return src.browse(source, operation, **params)
        raise SourceSearchError("unavailable")

    def status(self) -> dict:
        return {
            "sources": list(self._sources.keys()),
            "errors": dict(self._errors),
        }

    def close(self) -> None:
        """关闭线程池并释放全部音源（程序退出/模块停止时调用，幂等）。"""
        with self._life_lock:
            self._closed = True
            sources = list(self._sources.values())
            self._sources.clear()
            for lease in self._source_leases.values():
                lease.retire()
            self._source_leases.clear()
        for src in sources:
            try:
                src.close()
            except Exception:
                pass
        self._paged_executor.shutdown(wait=False, cancel_futures=True)
        self._legacy_download_executor.shutdown(wait=False, cancel_futures=True)
        self._cache.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
