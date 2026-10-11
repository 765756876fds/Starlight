"""OnlineSourceManager：注册/搜索/播放/歌词。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional

from .cache import OnlineCache
from .models import OnlineTrack
from .source import MusicSource


class OnlineSourceManager:
    def __init__(self, cache_ttl: float = 300.0, max_workers: int = 4,
                 request_timeout: float = 5.0):
        self._sources: Dict[str, MusicSource] = {}
        self._cache = OnlineCache(ttl_seconds=cache_ttl)
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._timeout = request_timeout
        self._errors: Dict[str, str] = {}
        # 音源版本号：register 递增、unregister 单调递增（删除标记）。
        # 同名源被替换/注销后，在途旧搜索任务的结果必须被丢弃且不得写回缓存。
        self._source_versions: Dict[str, int] = {}
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

    def list_sources(self) -> List[str]:
        return list(self._sources.keys())

    def search_all(self, keyword: str, limit_per_source: int = 10
                   ) -> Dict[str, List[OnlineTrack]]:
        out: Dict[str, List[OnlineTrack]] = {}
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
                     version: int) -> List[OnlineTrack]:
        return src.search(kw, limit)

    def get_play_url(self, track: OnlineTrack) -> Optional[str]:
        # 定向路由：track.source_name 通常等于注册名（自定义 HTTP 源）。
        src = self._sources.get(track.source_name)
        if src is not None:
            try:
                return src.get_play_url(track)
            except Exception as e:
                self._errors[track.source_name] = str(e)
                return None
        # 定向 miss：track.source_name 可能是桥接源的内部音源名（如 js_bridge
        # 返回的"酷我音乐"），而非注册名。遍历已注册源取第一个可播放 URL
        # （旧行为语义，仅在此 miss 时触发，不拖累定向命中路径）。
        for s in self._sources.values():
            try:
                url = s.get_play_url(track)
                if url:
                    return url
            except Exception:
                continue
        return None

    def get_lyric(self, track: OnlineTrack) -> Optional[str]:
        src = self._sources.get(track.source_name)
        if not src:
            return None
        try:
            return src.get_lyric(track)
        except Exception:
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
