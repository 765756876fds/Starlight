"""OnlineTrack / SearchResult 数据模型。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OnlineTrack:
    source_name: str
    track_id: str
    title: str
    artist: str = ""
    album: str = ""
    duration_seconds: int = 0
    cover_url: str | None = None
    extra: dict = field(default_factory=dict)


class SearchTracks(list):
    """兼容旧歌曲列表，同时保留桥接内部各插件的本次搜索状态。"""

    def __init__(self, tracks=(), *, errors=None, successful=1):
        super().__init__(tracks)
        self.errors = dict(errors or {})
        self.successful = successful


class SearchResults(dict):
    """兼容 {音源: 歌曲列表}；错误属于本次请求，不读取历史全局错误。"""

    def __init__(self):
        super().__init__()
        self.errors = {}
        self.successful_sources = set()
