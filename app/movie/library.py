"""User-selected video folders and read-only access to MPV resume checkpoints."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unicodedata

VIDEO_EXTENSIONS = {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".ts"}


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".starlight-", suffix=".tmp", delete=False) as stream:
            name = stream.name
            json.dump(value, stream, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)  # Only the temporary file created above.


def title_key(title):
    return re.sub(r"[\s._·]+", "", unicodedata.normalize("NFKC", str(title))).casefold()


def local_metadata(path):
    stem = Path(path).stem
    year_match = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", stem)
    episode = re.search(r"[Ss](\d{1,2})[Ee](\d{1,3})", stem)
    title = stem[:year_match.start()].strip(" ._-([（") if year_match else stem
    if episode:
        title = stem[:episode.start()].strip(" ._-")
    return {"title": title.replace(".", " ") or stem,
            "year": int(year_match.group()) if year_match else None,
            "season": int(episode[1]) if episode else None,
            "episode": int(episode[2]) if episode else None,
            "kind": "tv" if episode else "unknown"}


def resume_position(path, watch_directory, *, ignore_path=False):
    """Inspect native checkpoints only. No player launch or checkpoint writes."""
    if not path or not watch_directory:
        return None
    # Never infer basename matching: two different folders may contain movie.mp4.
    for key in (os.path.basename(str(path)) if ignore_path else str(path),):
        name = hashlib.md5(key.encode("utf-8"), usedforsecurity=False).hexdigest().upper()
        try:
            with (Path(watch_directory) / name).open(encoding="utf-8") as stream:
                text = stream.read(65536)
            match = re.search(r"^start=([0-9.]+)$", text, re.M)
            position = float(match[1]) if match else None
            if position is not None and math.isfinite(position) and position > 0:
                return position
        except (OSError, ValueError):
            continue
    return None


class MovieLibrary:
    MAX_FILES = 20000

    def __init__(self, index_path, clock=time.time):
        self.index_path = Path(index_path)
        self.clock = clock
        self._lock = threading.RLock()
        self.entries = []
        try:
            data = json.loads(self.index_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("version") == 1:
                self.entries = [r for r in data.get("entries", []) if isinstance(r, dict) and r.get("path")]
        except (OSError, ValueError, TypeError):
            pass

    def scan(self, directories):
        """Index names/stat only; do not follow links or examine video contents."""
        with self._lock:
            old = {os.path.normcase(r["path"]): r for r in self.entries}
        found, issues, seen = [], [], set()
        stack = [(Path(p), 0) for p in directories if isinstance(p, str) and p.strip()]
        while stack and len(found) < self.MAX_FILES:
            folder, depth = stack.pop()
            try:
                if folder.is_symlink() or os.path.normcase(str(folder.absolute())) in seen:
                    continue
                seen.add(os.path.normcase(str(folder.absolute())))
                with os.scandir(folder) as children:
                    for child in children:
                        if child.is_symlink():
                            continue
                        if child.is_dir(follow_symlinks=False) and depth < 20:
                            stack.append((Path(child.path), depth + 1))
                        elif child.is_file(follow_symlinks=False) and Path(child.name).suffix.lower() in VIDEO_EXTENSIONS:
                            path = os.path.abspath(child.path)
                            previous = old.get(os.path.normcase(path), {})
                            found.append(dict(local_metadata(path), path=path, available=True,
                                              added_at=previous.get("added_at", self.clock())))
                            if len(found) >= self.MAX_FILES:
                                break
            except OSError:
                issues.append("部分目录无法读取，请检查磁盘连接或目录权限")
        if stack:
            issues.append("已达到单次扫描上限，部分目录尚未扫描")
        # Offline/removable folders never erase an earlier library record.
        indexed = {os.path.normcase(r["path"]): r for r in found}
        retained = [dict(r, available=False) for key, r in old.items() if key not in indexed]
        with self._lock:
            self.entries = sorted([*found, *retained], key=lambda r: r.get("added_at", 0), reverse=True)
            snapshot = [dict(row) for row in self.entries]
            atomic_json(self.index_path, {"version": 1, "entries": snapshot})
        return {"entries": snapshot, "count": len(found), "issues": sorted(set(issues))}

    def find(self, work):
        key = title_key(work.get("title", ""))
        year, season = work.get("year"), work.get("season")
        with self._lock:
            return [dict(r) for r in self.entries if r.get("available", True)
                    and title_key(r["title"]) == key
                    and (not year or not r.get("year") or r["year"] == year)
                    and (season is None or r.get("season") is None or r["season"] == season)]

    @staticmethod
    def continuing(history, watch_directory):
        out = []
        for row in history:
            if row.get("media_type") != "local":
                continue
            path = row.get("replay_value", "")
            if not path or not os.path.isfile(path):
                continue
            position = resume_position(path, watch_directory)
            if position is not None:
                out.append(dict(row, resume_seconds=position))
        return out
