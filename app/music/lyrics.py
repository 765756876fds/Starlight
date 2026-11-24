"""Line lyrics, optional word cues and untimed reading text."""
from dataclasses import dataclass, field
import os
import re

_TIME = r"(\d{1,2}):(\d{1,2})(?:[.:](\d{1,3}))?"
_TIMESTAMP_RE = re.compile(r"\[" + _TIME + r"\]")
_WORD_TAG = re.compile(r"<(?:" + _TIME + r"|(\d{1,8}),(\d{1,8}))>")
_META = re.compile(r"^\[[\w-]+:.*\]$", re.I)
_OFFSET = re.compile(r"^\[offset:([+-]?\d{1,8})\]$", re.I)


@dataclass(frozen=True)
class WordCue:
    first: int  # Python character range in the shaped line, end exclusive
    last: int
    start: float
    end: float | None


@dataclass
class LyricDocument:
    lines: list[tuple[float, str]] = field(default_factory=list)
    words: dict[int, tuple[WordCue, ...]] = field(default_factory=dict)
    plain: list[str] = field(default_factory=list)


def _ts_to_seconds(mm, ss, frac):
    return int(mm) * 60 + int(ss) + (int(frac) / 10 ** len(frac) if frac else 0.)


def _word_ranges(raw, line_time, offset):
    """Enhanced LRC uses absolute onsets; LX specifies line-relative start and duration."""
    markers = list(_WORD_TAG.finditer(raw))
    if not markers:
        return raw.strip(), ()
    clean = _WORD_TAG.sub("", raw)
    start_padding = len(clean) - len(clean.lstrip())
    text = clean.strip()
    cursor = markers[0].start() - start_padding
    cues = []
    previous = -float("inf")
    relative = markers[0][4] is not None
    for i, m in enumerate(markers):
        if (m[4] is not None) != relative:
            return text, ()
        onset = line_time + int(m[4])/1000 if relative else _ts_to_seconds(*m.group(1, 2, 3)) + offset
        if onset < previous or onset < line_time - .002:
            return text, ()
        next_mark = markers[i+1] if i+1 < len(markers) else None
        span = raw[m.end():next_mark.start() if next_mark else None]
        stop = cursor + len(span)
        end = None
        if relative:
            end = onset + int(m[5])/1000
        elif next_mark and next_mark[4] is None:
            end = _ts_to_seconds(*next_mark.group(1, 2, 3)) + offset
        if end is not None and end < onset:
            return text, ()
        if span.strip() and min(len(text), stop) > max(0, cursor):
            cues.append(WordCue(max(0, cursor), min(len(text), stop), max(0., onset),
                                max(0., end) if end is not None else None))
        previous = onset
        cursor = stop
    return text, tuple(cues)


def parse_document(text: str | None) -> LyricDocument:
    rows = []
    plain = []
    offset = 0.
    for raw in (text or "").lstrip("\ufeff").splitlines():
        line = raw.strip()
        if not line:
            continue
        adjustment = _OFFSET.fullmatch(line)
        if adjustment:
            offset = int(adjustment[1])/1000.
            continue
        stamps = list(_TIMESTAMP_RE.finditer(line))
        if not stamps:
            if not _META.fullmatch(line):
                plain.append(line)
            continue
        body = _TIMESTAMP_RE.sub("", line)
        for stamp in stamps:
            onset = _ts_to_seconds(*stamp.groups()) + offset
            content, words = _word_ranges(body, onset, offset)
            if content:
                rows.append((max(0., onset), content, words))
    rows.sort(key=lambda row: row[0])
    return LyricDocument([(start, content) for start, content, _ in rows],
                         {i: words for i, (_, _, words) in enumerate(rows) if words},
                         [] if rows else plain)


def parse_lrc(text: str) -> list[tuple[float, str]]:
    """Line-only view for the lyric index and download validation."""
    return parse_document(text).lines


def plugin_lyric_text(ordinary, detailed):
    """Keep the complete primary lyric; only replace matching lines with real LX timestamps."""
    ordinary = ordinary if isinstance(ordinary, str) else ""
    if not isinstance(detailed, str) or not detailed:
        return ordinary or None
    rich = parse_document(detailed)
    if not rich.words:
        return ordinary or None
    if not parse_lrc(ordinary):
        return detailed
    # Compare parsed, offset-adjusted rows, then keep primary metadata and untouched lines.
    def key(start, content):
        return round(start * 1000), content
    additions = {key(*rich.lines[i]): spans for i, spans in rich.words.items()}
    out = []
    offset = 0.
    for line in ordinary.splitlines():
        tag = _OFFSET.fullmatch(line.strip())
        if tag:
            offset = int(tag[1])/1000.
        stamps = list(_TIMESTAMP_RE.finditer(line))
        if len(stamps) == 1:
            stamp = stamps[0]
            start = _ts_to_seconds(*stamp.groups()) + offset
            content, existing = _word_ranges(_TIMESTAMP_RE.sub("", line), start, offset)
            spans = additions.get(key(start, content))
            if spans and not existing and all(w.end is not None for w in spans):
                chunks = []
                last = 0
                for word in spans:
                    chunks.extend((content[last:word.first],
                                   f"<{max(0, round((word.start-start)*1000))},{round((word.end-word.start)*1000)}>",
                                   content[word.first:word.last]))
                    last = word.last
                line = stamp[0] + "".join(chunks) + content[last:]
        out.append(line)
    return "\n".join(out)



def find_local_lrc(audio_path: str | None) -> str | None:
    """查同名 .lrc：xxx.mp3 → 同目录 xxx.lrc。不存在/路径非法返回 None。"""
    if not audio_path:
        return None
    base, _ext = os.path.splitext(audio_path)
    lrc_path = base + ".lrc"
    if os.path.isfile(lrc_path):
        return lrc_path
    return None


def load_lyrics_from_file(lrc_path: str) -> list[tuple[float, str]]:
    """读 .lrc 文件（UTF-8 优先，GBK 回退）并解析。读失败返回 []。"""
    try:
        text = _read_text_tolerant(lrc_path)
    except Exception:
        return []
    return parse_lrc(text)


def load_lyric_document(path: str) -> LyricDocument:
    try:
        return parse_document(_read_text_tolerant(path))
    except OSError:
        return LyricDocument()


def _read_text_tolerant(path: str) -> str:
    """UTF-8 strict → GBK 回退（常见中文 .lrc 用 GBK 保存）。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except (UnicodeDecodeError, OSError):
        with open(path, "r", encoding="gbk", errors="replace") as f:
            return f.read()
