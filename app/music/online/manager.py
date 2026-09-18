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

    def register(self, source: MusicSource) -> None:
        self._sources[source.name] = source

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
        s = self._sources.pop(name, None)
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
            futs[self._executor.submit(self._safe_search, src, keyword, limit_per_source)] = name
        try:
            for fut in as_completed(futs, timeout=self._timeout + 2):
                name = futs[fut]
                try:
                    tracks = fut.result(timeout=self._timeout)
                    out[name] = tracks
                    self._cache.set(name, keyword, limit_per_source, tracks)
                except Exception as e:
                    self._errors[name] = str(e)
                    out[name] = []
        except Exception as e:
            for fut, name in futs.items():
                if not fut.done():
                    out[name] = []
                    self._errors[name] = f"timeout: {e}"
        return out

    def _safe_search(self, src: MusicSource, kw: str, limit: int) -> List[OnlineTrack]:
        return src.search(kw, limit)

    def get_play_url(self, track: OnlineTrack) -> Optional[str]:
        src = self._sources.get(track.source_name)
        if not src:
            return None
        try:
            return src.get_play_url(track)
        except Exception as e:
            self._errors[track.source_name] = str(e)
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
