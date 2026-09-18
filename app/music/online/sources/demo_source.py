"""Demo online source：内置少量公开示例曲目，证明架构可用。
不硬编码任何平台凭据。实际播放 URL 需要用户配置本地 mpv 可访问的音频。
"""
from __future__ import annotations

from typing import List, Optional

from ..models import OnlineTrack
from ..source import MusicSource


_DEMO_CATALOG = [
    OnlineTrack(source_name="demo", track_id="demo-1",
                title="示例曲目一", artist="Demo Artist", duration_seconds=180,
                extra={"url": ""}),
    OnlineTrack(source_name="demo", track_id="demo-2",
                title="示例曲目二", artist="Demo Band", duration_seconds=240,
                extra={"url": ""}),
]


class DemoSource(MusicSource):
    name = "demo"
    display_name = "Demo 音乐源（内置示例）"

    def search(self, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        kw = (keyword or "").lower().strip()
        if not kw:
            return list(_DEMO_CATALOG[:limit])
        return [t for t in _DEMO_CATALOG
                if kw in t.title.lower() or kw in t.artist.lower()][:limit]

    def get_play_url(self, track: OnlineTrack) -> Optional[str]:
        return track.extra.get("url") or None

    def get_lyric(self, track: OnlineTrack) -> Optional[str]:
        return None
