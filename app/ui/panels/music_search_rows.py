"""Independent search result ordering after recording display grouping."""
from collections import defaultdict
from dataclasses import dataclass
from itertools import zip_longest

from ...music.online.search_resources import SearchResource, search_resources, source_play_count


@dataclass
class RankedSearchRow:
    tracks: list
    size_resource: SearchResource | None
    preferred_resource: SearchResource | None
    play_count: int | None

    @property
    def track(self):
        return self.tracks[0]


def search_rows(groups, mode):
    rows = []
    for tracks in groups:
        options = search_resources(tracks[0])
        resources = options + tuple(resource for track in tracks[1:] for resource in search_resources(track))
        largest = max((resource for resource in resources if resource.size_bytes is not None),
                      key=lambda resource: resource.size_bytes, default=None)
        rows.append(RankedSearchRow(tracks, largest, options[-1] if options else None, source_play_count(tracks[0])))
    if mode == "size":
        known = [row for row in rows if row.size_resource is not None]
        rows = sorted(known, key=lambda row: -row.size_resource.size_bytes) + [
            row for row in rows if row.size_resource is None]
    elif mode == "heat":
        buckets, unknown = defaultdict(list), []
        for row in rows:
            if row.play_count is None:
                unknown.append(row)
            else:
                buckets[row.track.source_name].append(row)
        for bucket in buckets.values():
            bucket.sort(key=lambda row: -row.play_count)
        rows = [row for tier in zip_longest(*buckets.values()) for row in tier if row is not None] + unknown
    return rows


def resource_note(row, mode):
    if mode == "heat":
        return f"来源内 {row.play_count:,} 次" if row.play_count is not None else "热度未知"
    resource = row.size_resource if mode == "size" else row.preferred_resource
    text = resource.brief() if resource else ""
    if text:
        return ("最大 · " if mode == "size" and len(row.tracks) > 1 else "") + "标注 " + text
    return "大小未知" if mode == "size" else ""
