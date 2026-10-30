"""歌单播放：在线歌已下载 → 优先播本地副本；未下载 → 原在线重搜（不变）。"""
import pytest

pytestmark = pytest.mark.unit


import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch


def _make_ctrl(tmp_path):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_plp",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    c.mpv.loadfile.return_value = {"error": "success"}  # loadfile 协议契约
    c.set_movie_pauser(MagicMock(return_value=True))
    c._mpv_available = True
    return c


def _playlist_item_online(title="夜曲", artist="周杰伦", tid="t1", source="酷我音乐"):
    return {"title": title, "artist": artist, "source_type": "online",
            "source_name": source, "track_id": tid, "music_id": 0, "id": 1}


def _mk_track(title="夜曲", artist="周杰伦", tid="new-1", source="酷我音乐"):
    from app.music.online.models import OnlineTrack
    return OnlineTrack(source_name=source, track_id=tid, title=title, artist=artist)


def test_downloaded_playlist_item_plays_local(tmp_path):
    """已下载 → 播本地副本（find_download_path 命中 → db 行 → play_song），不重搜。"""
    c = _make_ctrl(tmp_path)
    local_path = str(tmp_path / "夜曲 - 周杰伦.mp3")
    c.db.insert_song({"path": local_path, "filename": "夜曲 - 周杰伦.mp3",
                      "title": "夜曲", "artist": "周杰伦", "album": "",
                      "duration": 0, "format": "mp3", "filesize": 10,
                      "last_modified": 0})
    dl = MagicMock()
    dl.find_download_path.return_value = local_path
    c.downloader = dl
    c.online_manager = MagicMock()
    with patch("os.path.exists", return_value=True):
        ok = c.play_playlist_item(_playlist_item_online())
    assert ok is True
    assert c.mpv.loadfile.call_count == 1
    assert c.mpv.loadfile.call_args.args[0] == local_path
    c.online_manager.search_all.assert_not_called()
    dl.find_download_path.assert_called_once_with("夜曲", "周杰伦")


def test_not_downloaded_playlist_item_researches(tmp_path):
    """未下载 → 原在线重搜（find_download_path 空 → play_search_result）。"""
    c = _make_ctrl(tmp_path)
    dl = MagicMock()
    dl.find_download_path.return_value = ""
    c.downloader = dl
    c.online_manager = MagicMock()
    c.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    c.online_manager.get_play_url.return_value = {"url": "http://x/new.mp3", "headers": None}
    ok = c.play_playlist_item(_playlist_item_online())
    assert ok is True
    c.online_manager.search_all.assert_called_once()
    assert c.mpv.loadfile.call_args.args[0] == "http://x/new.mp3"


def test_downloaded_but_file_missing_researches(tmp_path):
    """判定已下载但磁盘文件没了 → 走在线重搜（不播不存在文件）。"""
    c = _make_ctrl(tmp_path)
    dl = MagicMock()
    dl.find_download_path.return_value = str(tmp_path / "夜曲 - 周杰伦.mp3")  # 不存在
    c.downloader = dl
    c.online_manager = MagicMock()
    c.online_manager.search_all.return_value = {"酷我音乐": [_mk_track()]}
    c.online_manager.get_play_url.return_value = {"url": "http://x/n.mp3", "headers": None}
    ok = c.play_playlist_item(_playlist_item_online())
    assert ok is True
    c.online_manager.search_all.assert_called_once()


def test_local_playlist_item_unaffected(tmp_path):
    """本地歌条目（music_id>0）→ 原路径不变，不碰 downloader。"""
    c = _make_ctrl(tmp_path)
    song = c.db.insert_song({"path": "C:/l.mp3", "filename": "l.mp3",
                             "title": "本地", "artist": "Z", "album": "",
                             "duration": 0, "format": "mp3", "filesize": 1,
                             "last_modified": 0})
    c.downloader = MagicMock()
    with patch("os.path.exists", return_value=True):
        ok = c.play_playlist_item({"title": "本地", "artist": "Z",
                                   "source_type": "local", "music_id": song})
    assert ok is True
    c.downloader.find_download_path.assert_not_called()
    assert c.mpv.loadfile.call_args.args[0] == "C:/l.mp3"
