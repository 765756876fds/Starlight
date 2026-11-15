"""Private SQLite lifetime, rollback, bounded growth and explicit learning."""

from contextlib import closing
import sqlite3
import threading
import time

import pytest

from app.music.database import MusicDatabase
from app.music.preferences import PreferenceStore
from app.voice.interaction_memory import InteractionMemory


def record(memory, event, **kw):
    return memory.record(
        event, feature="evaluate:current:negative:", domain="music", action="next", **kw
    )


def test_revocable_feedback_survives_restart_without_overwriting_explicit(tmp_path):
    path = tmp_path / "interaction.db"
    m = InteractionMemory(path)
    explicit = PreferenceStore(tmp_path / "music-preferences.db")
    assert explicit.set_preference("track", "a", 1)
    assert record(m, "negative", media_key="a", rating="negative", kind="weak")
    assert record(m, "neutral", media_key="b", rating="neutral", kind="weak")
    assert record(m, "like", media_key="a", rating="positive", kind="explicit")
    assert m.flush() and m.close()
    n = InteractionMemory(path)
    assert n.start() and n.flush()
    assert n.weak_profile("music") == {"a": -0.3, "b": -0.1}
    assert n.revoke("negative") and n.revoke("like") and n.flush()
    assert n.weak_profile("music") == {"b": -0.1}
    assert explicit.profile()["explicit"][("track", "a")] == 1
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT revoked FROM interactions WHERE id='like'").fetchone() == (0,)
    assert n.close()


def test_observations_are_not_user_approval_confirmations_are_deduplicated(tmp_path):
    m = InteractionMemory(tmp_path / "habit.db")
    for i in range(8):
        assert record(m, str(i), kind="observation")
    assert m.flush()
    feature = "evaluate:current:negative:"
    assert m.preferred_domain(feature, ["music", "movie"]) is None
    for _ in range(4):
        assert record(m, "same-confirmation", kind="confirmation")
    assert m.flush()
    assert m.preferred_domain(feature, ["music", "movie"]) is None
    for i in range(2):
        assert record(m, f"correct-{i}", kind="correction")
    assert m.flush()
    assert m.preferred_domain(feature, ["music", "movie"]) == "music"
    assert m.preferred_domain(feature, ["movie"]) is None
    assert m.close()


@pytest.mark.parametrize("damage", ["corrupt", "future", "directory"])
def test_unavailable_memory_preserves_original_bytes(tmp_path, damage):
    path = tmp_path / "ledger.db"
    if damage == "directory":
        path.mkdir()
        before = None
    elif damage == "corrupt":
        path.write_bytes(b"not-a-database-preserve-me")
        before = path.read_bytes()
    else:
        with closing(sqlite3.connect(path)) as db:
            db.execute("PRAGMA user_version=92")
            db.execute("CREATE TABLE private_data(value)")
            db.execute("INSERT INTO private_data VALUES('keep')")
            db.commit()
        before = path.read_bytes()
    m = InteractionMemory(path)
    assert m.start()
    assert not m.flush()
    assert not m.available and not record(m, "bad")
    assert m.weak_profile("music") == {}
    if before is not None:
        assert path.read_bytes() == before
    assert m.close()


def test_queue_limit_idle_exit_and_row_limit(tmp_path):
    m = InteractionMemory(tmp_path / "bounded.db")
    m.LIMIT = 5
    for i in range(12):
        assert record(m, str(i), kind="weak", media_key="a", rating="negative")
        assert m.flush()
    assert len(m._cache) == 5
    assert m.weak_profile("music")["a"] == -1.5
    worker = m._worker
    worker.join(1)
    assert not worker.is_alive()
    assert record(m, "restart") and m.flush()
    assert m.close() and not record(m, "closed")


@pytest.mark.parametrize("source", ["local", "online"])
def test_fixed_playlist_atomic_dedup_reopen_and_old_playlist_untouched(tmp_path, source):
    path = tmp_path / "music.db"
    db = MusicDatabase(str(path))
    pid = db.create_playlist("用户原歌单")
    song = dict(
        title="隔离歌曲", source_type=source, music_id=7, source_name="fixture", track_id="7"
    )
    assert db.add_to_playlist(pid, song)
    results = []
    workers = [
        threading.Thread(target=lambda: results.append(db.save_liked_song(song))) for _ in range(4)
    ]
    for thread in workers:
        thread.start()
    for thread in workers:
        thread.join(1)
        assert not thread.is_alive()
    assert all(results) and len(results) == 4
    db.close()
    db = MusicDatabase(str(path))
    liked = [p for p in db.get_playlists() if p["name"] == "我喜欢的音乐"]
    assert len(liked) == 1 and liked[0]["song_count"] == 1
    assert len(db.get_playlist_songs(pid)) == 1
    db.close()


def test_stale_save_rolls_back_both_creation_and_insert(tmp_path):
    db = MusicDatabase(str(tmp_path / "rollback.db"))
    checks = iter([True, False])
    assert not db.save_liked_song(dict(title="歌曲", music_id=1), lambda: next(checks))
    assert db.get_playlists() == []
    assert db.save_liked_song(dict(title="歌曲", music_id=1))
    db.close()


def test_disk_lock_never_blocks_memory_reads_or_voice_submission(tmp_path):
    m = InteractionMemory(tmp_path / "locked.db")
    m.start()
    assert m.flush()
    with closing(sqlite3.connect(m.path)) as db:
        db.execute("BEGIN EXCLUSIVE")
        start = time.monotonic()
        assert record(m, "a")
        assert m.weak_profile("music") == {}
        assert time.monotonic() - start < 0.1
        assert not m.flush(0.5)
        db.rollback()
    assert m.close()
