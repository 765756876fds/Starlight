"""MusicSource 抽象基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from .models import OnlineTrack


class MusicSource(ABC):
    name: str = "base"
    display_name: str = "Base"

    @abstractmethod
    def search(self, keyword: str, limit: int = 20) -> List[OnlineTrack]:
        ...

    @abstractmethod
    def get_play_url(self, track: OnlineTrack) -> Optional[str]:
        ...

    @abstractmethod
    def get_lyric(self, track: OnlineTrack) -> Optional[str]:
        ...

    def close(self) -> None:
        pass
