"""Search display groups and stricter replacement evidence for recordings."""
import math
import re
from dataclasses import dataclass

from .popular_charts import normalized
from .search_text import fold_han_spaces, search_text

_UNKNOWN = {"", "未知", "未知歌手", "群星", "various artists", "unknown", "unknown artist", "n/a"}
_ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
_MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_DISPLAY_MARKS = re.compile(r"[–—,:!?。，！？：·]")
_VARIANT_KEYS = ("version", "versionName", "subtitle", "versionType", "mixName")


def raw_track(track):
    extra = track.extra or {}
    raw = extra.get("_raw", extra.get("raw"))
    return raw if isinstance(raw, dict) else {}


def resource_identity(track):
    return (track.source_name, track.track_id) if track.source_name and track.track_id else None


def _identifiers(track):
    raw = raw_track(track)
    fields = (("isrc", ("isrc", "ISRC"), _ISRC),
              ("mbid", ("musicbrainzRecordingId", "mbRecordingId", "recording_mbid"), _MBID))
    matches = {}
    for kind, keys, pattern in fields:
        values = []
        for key in keys:
            value = raw.get(key)
            if not isinstance(value, str):
                continue
            value = value.strip()
            value = re.sub(r"[\s-]", "", value).upper() if kind == "isrc" else value.lower()
            if pattern.fullmatch(value):
                values.append(value)
        if values:
            matches[kind] = frozenset(values)
    return matches


def _version(track):
    raw = raw_track(track)
    return normalized(" · ".join(dict.fromkeys(raw[k].strip() for k in _VARIANT_KEYS
                                              if isinstance(raw.get(k), str) and raw[k].strip())))


def _display_key(text):
    text = _DISPLAY_MARKS.sub(" ", search_text(text))
    # Dashes vary in imports. Keep the delimiter while normalizing its spacing.
    text = re.sub(r"\s+-\s+", " ", text)
    return " ".join(fold_han_spaces(text).split())


def recording_key(track):
    """The existing exact metadata threshold for automatic replacement."""
    title, artist, album = (normalized(getattr(track, field, "") or "")
                            for field in ("title", "artist", "album"))
    duration = track.duration_seconds
    if (not title or artist in _UNKNOWN or not album or type(duration) not in (int, float)
            or not math.isfinite(duration) or duration <= 0):
        return None
    return title, artist, album, duration, _version(track)


def _id_evidence(left, right):
    if any(len(values) != 1 for values in (*left.values(), *right.values())):
        return False, True
    shared = left.keys() & right.keys()
    return (any(left[k] == right[k] for k in shared), any(left[k] != right[k] for k in shared))


def same_recording(track, candidate):
    """Automatic replacement preserves exact metadata and vetoes external conflicts."""
    key = recording_key(track)
    return bool(key and key == recording_key(candidate)
                and not _id_evidence(_identifiers(track), _identifiers(candidate))[1])


@dataclass(frozen=True)
class _DisplayEvidence:
    album: str
    duration: float
    identifiers: dict


def _compatible(left, right):
    same_id, conflict = _id_evidence(left.identifiers, right.identifiers)
    if conflict:
        return False
    if left.album and left.album == right.album and left.duration == right.duration:
        return True
    return same_id and abs(left.duration - right.duration) <= 2


def group_tracks(tracks):
    """Keep original provider objects. Require pairwise agreement inside each row."""
    tracks = list(tracks)
    groups, buckets, identities = [], {}, {}
    for track in tracks:
        title, artist = _display_key(track.title), _display_key(track.artist)
        duration = track.duration_seconds
        if (not title or artist in _UNKNOWN or type(duration) not in (int, float)
                or not math.isfinite(duration) or duration <= 0):
            continue
        evidence = _DisplayEvidence(_display_key(track.album), duration, _identifiers(track))
        key = title, artist, _version(track)
        identities[id(track)] = key, evidence
        buckets.setdefault(key, []).append(evidence)

    rows_by_key = {}
    for track in tracks:
        identity = identities.get(id(track))
        if identity is None:
            groups.append([track])
            continue
        key, evidence = identity
        if not evidence.identifiers:
            matching = [candidate for candidate in buckets[key]
                        if candidate.identifiers and _compatible(evidence, candidate)]
            if any(_id_evidence(left.identifiers, right.identifiers)[1]
                   for i, left in enumerate(matching) for right in matching[i + 1:]):
                groups.append([track])
                continue
        rows = rows_by_key.setdefault(key, [])
        matches = [(row, members) for row, members in rows
                   if all(_compatible(evidence, member) for member in members)]
        if len(matches) == 1:
            row, members = matches[0]
            row.append(track)
            members.append(evidence)
        else:
            row = [track]
            groups.append(row)
            rows.append((row, [evidence]))
    return groups


def equivalent_versions(track, alternatives):
    seen = {resource_identity(track)}
    candidates = []
    for candidate in alternatives:
        key = resource_identity(candidate)
        if key is not None and key not in seen and same_recording(track, candidate):
            seen.add(key)
            candidates.append(candidate)
    for candidate in candidates:
        if all(same_recording(candidate, other) for other in candidates if other is not candidate):
            yield candidate
