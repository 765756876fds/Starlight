"""MusicSource 抽象基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import OnlineTrack


class SourceSearchError(Exception):
    """可确定的搜索失败类别；unknown 不推断为网络或超时。"""

    def __init__(self, category="unknown"):
        self.category = category
        super().__init__(category)


class MusicSource(ABC):
    name: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def search(self, keyword: str, limit: int = 20) -> list[OnlineTrack]:
        ...

    @abstractmethod
    def get_play_url(self, track: OnlineTrack) -> str | None:
        ...

    @abstractmethod
    def get_lyric(self, track: OnlineTrack) -> str | None:
        ...

    def close(self) -> None:
        pass
