"""Small presentation-only sample of current popular song charts."""
from dataclasses import dataclass, field
import unicodedata

from .models import OnlineTrack

SOURCE_LIMIT = 3
TRACK_LIMIT = 12
HOT = ("热歌", "热门", "流行", "hit songs", "popular", "hot songs")
OTHER = ("新歌", "新曲", "飙升", "历史", "总榜", "热搜", "搜索", "年度", "经典", "new release", "all time")


def chart_sources(plugins):
    names = (p["name"] for p in plugins if p.get("name") and p.get("capabilities", {}).get("charts"))
    return list(dict.fromkeys(names))[:SOURCE_LIMIT]


def choose_chart(groups):
    choices = []
    for group in groups:
        for board in group.get("lists", []):
            name = str(board.get("title") or "").casefold()
            context = str(group.get("title") or "").casefold() + " " + name
            if board.get("id") and not any(word in context for word in OTHER):
                for priority, word in enumerate(HOT):
                    if word in name:
                        choices.append((priority, board))
                        break
    return min(choices, key=lambda pair: pair[0])[1] if choices else None


@dataclass(frozen=True)
class ChartEntry:
    track: OnlineTrack
    source: str
    board_id: str
    board: str
    rank: int


def read_chart(music, source, current):
    catalog = music.get_top_lists(source)
    if source not in catalog:
        raise ValueError("Chart directory unavailable")
    board = choose_chart(catalog[source])
    if not board or not current():
        return []
    payload = music.get_top_list_detail(source, board["id"], limit=TRACK_LIMIT)
    if not isinstance(payload, dict) or not isinstance(payload.get("tracks"), list):
        raise ValueError("Chart tracks unavailable")
    return [ChartEntry(t, source, board["id"], board["title"], rank)
            for rank, t in enumerate(payload["tracks"][:TRACK_LIMIT], 1)
            if isinstance(t, OnlineTrack) and t.title and t.track_id and t.source_name == source]


def normalized(value):
    return " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())


def version_label(track):
    raw = (track.extra or {}).get("_raw")
    if not isinstance(raw, dict):
        return ""
    return " · ".join(dict.fromkeys(raw[k].strip() for k in ("version", "versionName", "subtitle")
                                   if isinstance(raw.get(k), str) and raw[k].strip()))


@dataclass
class SongGroup:
    entries: list[ChartEntry] = field(default_factory=list)


def group_charts(batches):
    """Group for display only. Retain each original plugin object and chart position."""
    groups = {}
    for entries in batches:
        for entry in entries:
            t = entry.track
            key = (normalized(t.title), normalized(t.artist), normalized(t.album),
                   t.duration_seconds, normalized(version_label(t)))
            if key[1] in ("", "未知", "未知歌手", "群星", "various artists") or not key[2] or not t.duration_seconds:
                key += (entry.source, t.track_id)
            group = groups.setdefault(key, SongGroup())
            group.entries.append(entry)
    def sort_key(group):
        best = min(e.rank for e in group.entries)
        bonus = min(2, len({e.source for e in group.entries}) - 1)
        return best - bonus, best
    return sorted(groups.values(), key=sort_key)
