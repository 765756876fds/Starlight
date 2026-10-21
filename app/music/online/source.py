"""MusicSource 抽象基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from .models import OnlineTrack


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
