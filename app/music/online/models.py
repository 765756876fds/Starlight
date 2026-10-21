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
