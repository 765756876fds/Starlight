"""歌单：数据层（建/删/重命名/去重/级联/排序/持久化）+ controller（插队/重播/删文件）。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

from app.music.database import MusicDatabase


def _mkdb(tmp_path):
    return MusicDatabase(str(tmp_path / "pl.db"))


# ---------- 数据层 ----------

def test_create_rename_duplicate_rejected(tmp_path):
    """建歌单 + 重命名；重名（建/改名）拒绝。"""
    db = _mkdb(tmp_path)
    try:
        p1 = db.create_playlist("A")
        p2 = db.create_playlist("B")
        assert p1 is not None and p2 is not None
        assert db.create_playlist("A") is None          # 重名拒绝
        assert db.rename_playlist(p1, "C") is True
        assert db.rename_playlist(p1, "B") is False     # 改名撞 B → 拒绝
        names = [x["name"] for x in db.get_playlists()]
        assert names == ["C", "B"]
    finally:
        db.close()


def test_delete_playlist_cascades(tmp_path):
    """删歌单 → 歌单内歌级联删；重复删不崩。"""
    db = _mkdb(tmp_path)
    try:
        pid = db.create_playlist("P")
        db.add_to_playlist(pid, {"title": "T", "artist": "A",
                                 "source_type": "local", "music_id": 1})
        db.add_to_playlist(pid, {"title": "O", "artist": "B",
                                 "source_type": "online",
                                 "source_name": "kw", "track_id": "x"})
        db.delete_playlist(pid)
        assert db.get_playlist_songs(pid) == []
        assert db.get_playlists() == []
        db.delete_playlist(pid)  # 幂等
    finally:
        db.close()


def test_add_to_playlist_dedup(tmp_path):
    """去重：本地按 music_id、在线按 source_name+track_id；不同歌可加。"""
    db = _mkdb(tmp_path)
    try:
        pid = db.create_playlist("P")
        l1 = {"title": "T", "artist": "A", "source_type": "local", "music_id": 1}
        assert db.add_to_playlist(pid, l1) is True
        assert db.add_to_playlist(pid, l1) is False            # 本地同歌去重
        assert db.add_to_playlist(pid, dict(l1, music_id=2)) is True  # 不同本地歌
        o1 = {"title": "O", "artist": "B", "source_type": "online",
              "source_name": "kw", "track_id": "x1"}
        assert db.add_to_playlist(pid, o1) is True
        assert db.add_to_playlist(pid, o1) is False            # 在线同源同 track 去重
        assert db.add_to_playlist(pid, dict(o1, track_id="x2")) is True
        assert len(db.get_playlist_songs(pid)) == 4
    finally:
        db.close()


def test_playlist_sort_and_count(tmp_path):
    """歌单内歌按 added_at 排序；get_playlists 带 song_count。"""
    db = _mkdb(tmp_path)
    try:
        pid = db.create_playlist("P")
        db.add_to_playlist(pid, {"title": "A", "source_type": "local", "music_id": 1})
        db.add_to_playlist(pid, {"title": "B", "source_type": "local", "music_id": 2})
        rows = db.get_playlist_songs(pid)
        assert [r["title"] for r in rows] == ["A", "B"]
        pls = db.get_playlists()
        assert pls[0]["song_count"] == 2
        assert pls[0]["name"] == "P"
    finally:
        db.close()


def test_remove_from_playlist(tmp_path):
    """从歌单移除（按 playlist_songs.id）；已移除再移除返回 False。"""
    db = _mkdb(tmp_path)
    try:
        pid = db.create_playlist("P")
        db.add_to_playlist(pid, {"title": "T", "source_type": "local", "music_id": 1})
        psid = db.get_playlist_songs(pid)[0]["id"]
        assert db.remove_from_playlist(pid, psid) is True
        assert db.remove_from_playlist(pid, psid) is False
        assert db.get_playlist_songs(pid) == []
    finally:
        db.close()


def test_playlist_persist(tmp_path):
    """持久化：关库重开，歌单与歌还在。"""
    p = str(tmp_path / "persist.db")
    db = MusicDatabase(p)
    pid = db.create_playlist("持久")
    db.add_to_playlist(pid, {"title": "T", "source_type": "local", "music_id": 1})
    db.close()
    db2 = MusicDatabase(p)
    try:
        assert [x["name"] for x in db2.get_playlists()] == ["持久"]
        assert len(db2.get_playlist_songs(pid)) == 1
        assert db2.get_playlist_songs(pid)[0]["music_id"] == 1
    finally:
        db2.close()


# ---------- controller ----------

def _make_ctrl(tmp_path):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_pl",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    return c


def test_add_next_inserts_after_current(tmp_path):
    """下一首播放：插到当前播放位置之后。"""
    c = _make_ctrl(tmp_path)
    c.queue.add_all([{"title": "A"}, {"title": "B"}, {"title": "C"}])
    c.queue.play_at(1)  # B 播放中
    c.add_next({"title": "D"})
    assert [s["title"] for s in c.queue.get_queue()] == ["A", "B", "D", "C"]


def test_add_next_no_current_inserts_front(tmp_path):
    """无播放（current=-1）→ 插队首。"""
    c = _make_ctrl(tmp_path)
    c.queue.add_all([{"title": "A"}])
    c.add_next({"title": "X"})
    assert [s["title"] for s in c.queue.get_queue()] == ["X", "A"]


def test_play_playlist_item_local_ok(tmp_path):
    """歌单本地歌：按 music_id 找到 → play_song + True。"""
    c = _make_ctrl(tmp_path)
    song = {"id": 5, "path": "C:/fake/ok.mp3", "title": "T", "artist": "A"}
    c.db.get_song_by_id = MagicMock(return_value=song)
    with patch("os.path.exists", return_value=True):
        with patch.object(c, "play_song") as ps:
            ok = c.play_playlist_item({"music_id": 5, "title": "T", "artist": "A"})
    assert ok is True
    ps.assert_called_once_with(song)


def test_play_playlist_item_local_missing(tmp_path):
    """歌单本地歌：music_id 找不到 → False，不崩。"""
    c = _make_ctrl(tmp_path)
    assert c.play_playlist_item({"music_id": 99999, "title": "无", "artist": "x"}) is False


def test_play_playlist_item_online_not_found(tmp_path):
    """歌单在线歌：重搜不到 → False，不崩。"""
    c = _make_ctrl(tmp_path)
    c.search = MagicMock()
    c.search.search.return_value = []
    c.online_manager = MagicMock()
    c.online_manager.search_all.return_value = {}
    assert c.play_playlist_item({"music_id": 0, "title": "搜不到", "artist": "y"}) is False


def test_add_to_playlist_no_source_type_defaults_local(tmp_path):
    """回归 Bug1+2：本地列表项是原始 dict（无 source_type 键）→ 默认按 local：
    两首不同本地歌都能加；插入的 source_type 是 'local' 而非 'online'。"""
    db = _mkdb(tmp_path)
    try:
        pid = db.create_playlist("P")
        # 本地歌 dict 无 source_type 键（music 表行），无 source_name/track_id
        l1 = {"id": 1, "title": "T1", "artist": "A1", "path": "C:/x/1.mp3"}
        l2 = {"id": 2, "title": "T2", "artist": "A2", "path": "C:/x/2.mp3"}
        assert db.add_to_playlist(pid, l1) is True   # 第一首
        assert db.add_to_playlist(pid, l2) is True   # 第二首不再被误判重复
        rows = db.get_playlist_songs(pid)
        assert len(rows) == 2
        assert rows[0]["source_type"] == "local" and rows[0]["music_id"] == 1
        assert rows[1]["source_type"] == "local" and rows[1]["music_id"] == 2
        # 同歌再加 → 去重拒绝
        assert db.add_to_playlist(pid, l1) is False
        # 在线歌显式 online 不受影响
        o1 = {"title": "O", "artist": "B", "source_type": "online",
              "source_name": "kw", "track_id": "x"}
        assert db.add_to_playlist(pid, o1) is True
        assert db.add_to_playlist(pid, o1) is False
        assert db.get_playlist_songs(pid)[2]["source_type"] == "online"
    finally:
        db.close()


def test_delete_local_file_removes_all_refs(tmp_path):
    """删除本地文件：删磁盘 + music 表 + 歌单引用；历史保留。"""
    c = _make_ctrl(tmp_path)
    f = tmp_path / "del.mp3"
    f.write_bytes(b"x")
    song = {"id": 1, "path": str(f), "filename": "del.mp3", "title": "T", "artist": "A"}
    song["id"] = c.db.insert_song(song)  # AUTOINCREMENT：以实际 rowid 为准
    pid = c.db.create_playlist("P")
    c.db.add_to_playlist(pid, {"title": "T", "artist": "A",
                               "source_type": "local", "music_id": 1})
    c.db.add_history(title="T", artist="A", source_type="local", music_id=1)
    ok, err = c.delete_local_file(song)
    assert ok is True and err == ""
    assert not f.exists()                      # 磁盘已删
    assert c.db.get_song_by_id(1) is None      # 库已移除（FTS 触发器同步）
    assert c.db.get_playlist_songs(pid) == []  # 歌单引用已清
    assert len(c.db.get_history()) == 1        # 历史保留


def test_delete_local_file_failure_keeps_db(tmp_path):
    """文件删除失败（占用/权限）→ 返回错误、库不动。"""
    c = _make_ctrl(tmp_path)
    f = tmp_path / "lock.mp3"
    f.write_bytes(b"x")
    song = {"id": 2, "path": str(f), "filename": "lock.mp3", "title": "T"}
    song["id"] = c.db.insert_song(song)  # AUTOINCREMENT：以实际 rowid 为准
    with patch("os.remove", side_effect=PermissionError("busy")):
        ok, err = c.delete_local_file(song)
    assert ok is False and "busy" in err
    assert c.db.get_song_by_id(song["id"]) is not None  # 记录未删
    assert f.exists()
