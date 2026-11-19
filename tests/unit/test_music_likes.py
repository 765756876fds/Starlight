"""The shared liked playlist, exact membership and persistence."""
import sqlite3

import pytest

from app.music.database import MusicDatabase


def test_like_and_unlike_survive_reopening_without_touching_other_members(tmp_path):
    path = tmp_path / "music.db"
    file_path = tmp_path / "song.wav"
    file_path.write_bytes(b"test fixture")
    db = MusicDatabase(str(path))
    local_id = db.insert_song(dict(path=str(file_path), filename=file_path.name, title="同名"))
    local = dict(id=local_id, title="同名")
    online = dict(id=-1, source="online", source_name="First", track_id="9", title="同名")
    other = dict(online, source_type="online", source_name="Second")
    normal = db.create_playlist("另一个歌单")
    assert db.add_to_playlist(normal, local)
    assert db.add_to_playlist(normal, dict(online, source_type="online"))
    assert db.is_liked_song(local) is False
    assert [p["id"] for p in db.get_playlists()] == [normal]
    assert db.save_liked_song(local)
    assert db.set_liked_song(online, True)
    assert db.set_liked_song(dict(online, title="不同标题"), True)
    assert db.set_liked_song(other, True)
    db.close()

    db = MusicDatabase(str(path))
    liked, = [p for p in db.get_playlists() if p["name"] == "我喜欢的音乐"]
    assert liked["song_count"] == 3
    assert db.is_liked_song(dict(local, title="改名")) is True
    assert db.is_liked_song(online) is True
    assert db.set_liked_song(local, False)
    assert db.set_liked_song(online, False)
    assert db.set_liked_song(online, False)
    db.close()

    db = MusicDatabase(str(path))
    try:
        assert db.is_liked_song(local) is False
        assert db.is_liked_song(online) is False
        assert db.is_liked_song(other) is True
        assert len(db.get_playlist_songs(normal)) == 2
        assert db.get_song_by_id(local_id)["path"] == str(file_path)
        assert file_path.read_bytes() == b"test fixture"
    finally:
        db.close()


@pytest.mark.parametrize("song", [
    dict(id=-1, title="缺音源"),
    dict(id=1, source="online", track_id="7", title="缺音源"),
    dict(id=1, source_type="online", source_name="First", title="缺曲目 ID"),
    dict(id=1, online=True, source_name="First", track_id=" ", title="空曲目 ID"),
])
def test_unknown_identity_does_not_create_or_change_playlist(music_db, song):
    assert music_db.is_liked_song(song) is None
    assert not music_db.set_liked_song(song, True)
    assert not music_db.set_liked_song(song, False)
    assert not music_db.get_playlists()


def test_locked_write_keeps_membership_and_can_be_retried(music_db):
    song = dict(id=1, title="歌曲")
    assert music_db.save_liked_song(song)
    other = sqlite3.connect(music_db.db_path)
    try:
        other.execute("BEGIN IMMEDIATE")
        assert not music_db.set_liked_song(song, False)
        assert music_db.is_liked_song(song) is True
    finally:
        other.close()
    assert music_db.set_liked_song(song, False)
    assert music_db.is_liked_song(song) is False
