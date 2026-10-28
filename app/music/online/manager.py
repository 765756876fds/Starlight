"""OnlineSourceManager：注册/搜索/播放/歌词。"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from app.core.logger import get_logger

from .cache import OnlineCache
from .models import OnlineTrack
from .source import MusicSource

logger = get_logger()


class OnlineSourceManager:
    def __init__(self, cache_ttl: float = 300.0, max_workers: int = 4,
                 request_timeout: float = 5.0):
        self._sources: dict[str, MusicSource] = {}
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

    def register(self, source: MusicSource) -> None:
        with self._life_lock:
            self._sources[source.name] = source
            self._source_versions[source.name] = self._source_versions.get(source.name, 0) + 1

    def load_custom_sources(self, directory: str) -> int:
        """扫描目录下 *.py，每个文件必须定义 create_source() -> MusicSource。"""
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

    def get_source(self, name: str) -> MusicSource | None:
        """按注册名取源（UI 等外部组件用，避免摸私有 dict）。"""
        return self._sources.get(name)

    def invalidate(self, name: str) -> None:
        """清除某源的搜索缓存（桥接/插件重载后调用，避免旧结果掩盖新注册表）。

        只删缓存不注销源；生命周期锁内与 search_all 的版本校验+写入互斥，
        不会让新缓存写入与清除交错。
        """
        with self._life_lock:
            self._cache.delete(name)

    def search_all(self, keyword: str, limit_per_source: int = 10
                   ) -> dict[str, list[OnlineTrack]]:
        out: dict[str, list[OnlineTrack]] = {}
        futs = {}
        for name, src in self._sources.items():
            cached = self._cache.get(name, keyword, limit_per_source)
            if cached is not None:
                out[name] = cached
                continue
            ver = self._source_versions.get(name, 0)
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
                        if self._source_versions.get(name) != ver:
                            continue
                        out[name] = tracks
                        self._cache.set(name, keyword, limit_per_source, tracks)
                except Exception as e:
                    self._errors[name] = str(e)
                    out[name] = []
        except Exception as e:
            for fut, (name, ver) in futs.items():
                if not fut.done():
                    out[name] = []
                    self._errors[name] = f"timeout: {e}"
        return out

    def _safe_search(self, src: MusicSource, kw: str, limit: int,
                     version: int) -> list[OnlineTrack]:
        t0 = time.monotonic()
        try:
            return src.search(kw, limit)
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

    def get_play_url(self, track: OnlineTrack, quality: str | None = None) -> dict | None:
        # 定向路由：track.source_name 通常等于注册名（自定义 HTTP 源）。
        tried = set()
        src = self._sources.get(track.source_name)
        if src is not None:
            tried.add(id(src))
            try:
                r = self._normalize_play_result(self._call_get_url(src, track, quality))
                if r:
                    return r
            except Exception as e:
                self._errors[track.source_name] = str(e)
                logger.warning(f"定向源 {track.source_name} 失败，回退其他源: {e}")
        # 定向 miss 或定向失败：遍历 accepts 命中的源（不重复定向源）
        for s in self._sources.values():
            if id(s) in tried:
                continue
            try:
                acc = getattr(s, "accepts", None)
                if acc is None or not acc(track):
                    continue
                tried.add(id(s))
                r = self._normalize_play_result(self._call_get_url(s, track, quality))
                if r:
                    return r
            except Exception as e:
                logger.warning(f"获取在线源失败: {e}")
                continue
        # 仍 miss：回退全遍历（不重复已试源）
        for s in self._sources.values():
            if id(s) in tried:
                continue
            try:
                r = self._normalize_play_result(self._call_get_url(s, track, quality))
                if r:
                    return r
            except Exception:
                continue
        return None

    @staticmethod
    def _normalize_play_result(r):
        """str 型源（http/demo/archive）包装成 dict；dict 型（js_bridge）原样。"""
        if not r:
            return None
        if isinstance(r, dict):
            return r
        return {"url": r, "headers": None}

    def get_lyric(self, track: OnlineTrack) -> str | None:
        # 定向路由：track.source_name 通常等于注册名（自定义 HTTP 源）。
        src = self._sources.get(track.source_name)
        if src is not None:
            try:
                return src.get_lyric(track)
            except Exception:
                return None
        # 定向 miss：track.source_name 可能是 js_bridge 的内部插件名（如"酷狗音乐"），
        # 而非注册名（"js_bridge"）。对齐 get_play_url：accepts 命中源优先路由，
        # 避免遍历 http 源对陌生 track 发无谓请求。不加全遍历——歌词无强回退需求。
        for s in self._sources.values():
            try:
                acc = getattr(s, "accepts", None)
                if acc is None or not acc(track):
                    continue
                r = s.get_lyric(track)
                if r:
                    return r
            except Exception:
                continue  # 单源失败继续下一个
        return None

    def get_top_lists(self) -> dict[str, list]:
        """聚合各源榜单分组（源自报能力，无该方法/失败自动跳过）。

        返回 {源名或插件名: [分组]}——js_bridge 内部按插件名分组返回；
        自定义源若实现 get_top_lists 同样聚合。失败记录 _errors 不抛。
        """
        out: dict[str, list] = {}
        for name, src in self._sources.items():
            fn = getattr(src, "get_top_lists", None)
            if not callable(fn):
                continue
            try:
                r = fn()
                if r:
                    out.update(r)
            except Exception as e:
                self._errors[name] = str(e)
        return out

    def get_top_list_detail(self, source: str, list_id: str,
                            page: int = 1) -> dict | None:
        """单榜单详情：路由到实现 get_top_list_detail 的源（js_bridge 按插件名）。

        返回 {is_end: bool, tracks: [OnlineTrack]}；无能力/失败 → None。
        """
        for name, src in self._sources.items():
            fn = getattr(src, "get_top_list_detail", None)
            if not callable(fn):
                continue
            try:
                r = fn(source, list_id, page)
                if r is not None:
                    return r
            except Exception as e:
                self._errors[name] = str(e)
        return None

    def status(self) -> dict:
        return {
            "sources": list(self._sources.keys()),
            "errors": dict(self._errors),
        }

    def close(self) -> None:
        """关闭线程池并释放全部音源（程序退出/模块停止时调用，幂等）。"""
        for name, src in list(self._sources.items()):
            try:
                src.close()
            except Exception:
                pass
        self._sources.clear()
        self._cache.clear()
        self._executor.shutdown(wait=False, cancel_futures=True)
