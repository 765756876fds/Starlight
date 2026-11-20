"""Relevance for manual search display. Tracks retain their provider identity."""

import re
import unicodedata
from collections.abc import Iterable

from ...music.online.models import OnlineTrack


_FORMATTING = re.compile(
    r'[()\[\]【】《》“”"–—·]|\s+[-+]\s+|(?<=[\u3400-\u9fff])-(?=[\u3400-\u9fff])'
)
_HAN_SPACES = re.compile(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])")
_HAN_TITLE = re.compile(r"[\u3400-\u9fff]{2,}")
_TRAILING_NOTES = re.compile(r"(?:\s*(?:\([^()]+\)|\[[^\[\]]+\]|【[^】]+】))+$")


def _query_key(value: str) -> str:
    text = _FORMATTING.sub(" ", value)
    return " ".join(_HAN_SPACES.sub("", text).split())


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold().strip()


def _matches(query: str, title: str, artist: str) -> bool:
    return bool(title) and (query == _query_key(title) or (
        bool(artist) and query in (_query_key(f"{title} {artist}"), _query_key(f"{artist} {title}"))
    ))


def rank_manual_search(tracks: Iterable[OnlineTrack], keyword: str) -> list[OnlineTrack]:
    """Prefer whole-field matches, then title evidence; leave ties in provider order."""
    query = _query_key(_normalized(keyword))
    if not any(char.isalnum() for char in query):
        return list(tracks)

    def rank(track: OnlineTrack) -> int:
        title = _normalized(track.title)
        artist = _normalized(track.artist)
        if _matches(query, title, artist):
            return 0
        # Trailing notes explain a partial match without assigning a preferred version.
        base = _TRAILING_NOTES.sub("", title)
        if base != title and _matches(query, base, artist):
            return 1
        title_key = _query_key(title)
        if len(query) > 1 and (
            (_HAN_TITLE.fullmatch(query) and query in title_key) or f" {query} " in f" {title_key} "
        ):
            return 2
        return 3

    return sorted(tracks, key=rank)
