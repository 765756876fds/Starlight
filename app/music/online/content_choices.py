"""Conservative display groups; every choice retains its original plugin object."""
import math
from .popular_charts import normalized, version_label


def recording_key(track):
    title, artist, album = (normalized(getattr(track, field, "") or "")
                            for field in ("title", "artist", "album"))
    duration = getattr(track, "duration_seconds", 0)
    if (not title or artist in ("", "未知", "未知歌手", "群星", "various artists")
            or not album or type(duration) not in (int, float)
            or not math.isfinite(duration) or duration <= 0):
        return None
    return title, artist, album, duration, normalized(version_label(track))


def group_tracks(tracks):
    """Keep search relevance order, never infer recording identity from title alone."""
    groups, indices = [], {}
    for track in tracks:
        key = recording_key(track)
        if key is not None and key in indices:
            groups[indices[key]].append(track)
        else:
            if key is not None:
                indices[key] = len(groups)
            groups.append([track])
    return groups


def equivalent_versions(track, alternatives):
    key = recording_key(track)
    seen = {(track.source_name, track.track_id)}
    for candidate in alternatives:
        identity = candidate.source_name, candidate.track_id
        if key is not None and recording_key(candidate) == key and identity not in seen:
            seen.add(identity)
            yield candidate
