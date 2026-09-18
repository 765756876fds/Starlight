"""Re-scan highlights using pychorus."""
from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

TARGET = 35.0


def detect(audio_path: str):
    try:
        from pychorus import find_and_output_chorus
        start = find_and_output_chorus(audio_path, None, clip_length=TARGET)
        if start is None or start < 0:
            return None
        return (float(start), float(start + TARGET))
    except Exception as e:
        print(f"[rescan] fail {audio_path}: {e}")
        return None


def main():
    db = sqlite3.connect(str(ROOT / "data" / "music.db"))
    # manual 列兼容
    try:
        db.execute("ALTER TABLE music_highlight ADD COLUMN manual INTEGER DEFAULT 0")
        db.commit()
    except Exception:
        pass
    rows = db.execute("SELECT id, path, title FROM music").fetchall()
    print(f"[rescan] total={len(rows)}")
    ok = fail = skip = 0
    for sid, path, title in rows:
        existing = db.execute(
            "SELECT manual FROM music_highlight WHERE song_id=?", (sid,)
        ).fetchone()
        if existing and existing[0]:
            skip += 1
            continue
        if not path or not os.path.exists(path):
            fail += 1
            continue
        r = detect(path)
        if r is None:
            fail += 1
            continue
        start, end = r
        db.execute("DELETE FROM music_highlight WHERE song_id=?", (sid,))
        db.execute(
            "INSERT INTO music_highlight (song_id, start_time, end_time, confidence, source, manual) "
            "VALUES (?, ?, ?, 1.0, 'pychorus', 0)",
            (sid, start, end),
        )
        ok += 1
        if ok <= 5:
            print(f"  [{sid}] {title}: {start:.1f}-{end:.1f}")
    db.commit()
    print(f"[rescan] done ok={ok} fail={fail} skip_manual={skip}")


if __name__ == "__main__":
    main()
