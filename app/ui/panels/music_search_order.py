"""Search relevance based on explicit title and performer evidence."""
import re
from collections.abc import Iterable

from ...music.online.models import OnlineTrack
from ...music.online.search_text import fold_han_spaces, search_text
from ...music.online.content_choices import raw_track

_FORMATTING = re.compile(
    r'[()\[\]【】《》“”"–—·、,!:?。，！？：]|\s+[-+]\s+|(?<=[\u3400-\u9fff])-(?=[\u3400-\u9fff])'
)
_HAN_TITLE = re.compile(r"[\u3400-\u9fff]{2,}")
_TRAILING_NOTES = re.compile(r"(?:\s*(?:\([^()]+\)|\[[^\[\]]+\]|【[^】]+】))+$")
_ARTIST_SEPARATOR = re.compile(r"\s+(?:&|/|feat\.?|ft\.?|featuring)\s+|[,、，；;]")


def _key(value):
    return " ".join(fold_han_spaces(_FORMATTING.sub(" ", value)).split())


def _aliases(value):
    if isinstance(value, str):
        value = (value,)
    if not isinstance(value, (tuple, list)):
        return ()
    return tuple(search_text(item) for item in value[:8] if isinstance(item, str) and item.strip())


def rank_manual_search(tracks: Iterable[OnlineTrack], keyword: str) -> list[OnlineTrack]:
    """Rank whole-field matches ahead of partial matches; preserve provider ties."""
    query = _key(search_text(keyword))
    if not any(char.isalnum() for char in query):
        return list(tracks)

    def rank(track):
        title, artist = search_text(track.title), search_text(track.artist)
        title_key = _key(title)
        raw = raw_track(track)
        performers = (artist, *_ARTIST_SEPARATOR.split(artist), *_aliases(raw.get("artistAliases")))
        artist_keys = {_key(name) for name in performers if name}
        base = _TRAILING_NOTES.sub("", title)
        aliases = _aliases(raw.get("aliases") or raw.get("alias"))

        def matches(value):
            return bool(value) and (query == _key(value) or any(
                query in (_key(f"{value} {name}"), _key(f"{name} {value}"))
                for name in performers if name))

        if matches(title):
            return 0
        if (base != title and matches(base)) or any(matches(alias) for alias in aliases):
            return 1
        if query in artist_keys and len(query) > 1:
            return 2
        if len(query) > 1 and (
            (_HAN_TITLE.fullmatch(query) and query in title_key) or f" {query} " in f" {title_key} "
        ):
            return 3
        return 4

    return sorted(tracks, key=rank)
