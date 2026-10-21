"""队列在线歌播放：直取优先（消费存的 track_id/raw_item），直取失败才重搜兜底；
失败主动跳过；本地歌不受影响。"""
import pytest

pytestmark = pytest.mark.unit


import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch


def _wait_until(condition, timeout=2.0):
    """轮询等待：适配切歌异步化（loadfile 在后台 worker 线程执行）。"""
    start = time.time()
    while time.time() - start < timeout:
        if condition():
            return True
        time.sleep(0.05)
    return False


def _make_ctrl(tmp_path):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_on2",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    c._mpv_available = True
    return c


def _online_song(name="夜曲", artist="周杰伦", tid="old-1", source="kw"):
    return {"title": name, "artist": artist, "source_type": "online",
            "source_name": source, "track_id": tid, "music_id": 0,
            "id": 0, "path": ""}


def _mk_track(name="夜曲", artist="周杰伦", tid="new-1", source="kw"):
    from app.music.online.models import OnlineTrack
    return OnlineTrack(source_name=source, track_id=tid, title=name, artist=artist)


def test_queue_online_song_direct_url_first(tmp_path):
    """直取优先：队列在线歌带 source_name+track_id → 直接用存的 ID 取流，
    不重搜（避免重搜命中片段版/试听版）；历史记录直取 track 的 id。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    # 直取成功（底层 get_play_url 消费 track_id/raw_item 返回直链）
    c.online_manager.get_play_url.return_value = {
        "url": "http://x/direct.mp3", "headers": None}
    c._play_song(_online_song())  # 存的 track_id 是 old-1
    # 直取用存的 track_id，不走 resolver
    got = c.online_manager.get_play_url.call_args.args[0]
    assert got.track_id == "old-1" and got.source_name == "kw"
    assert c._resolver.resolve.call_count == 0, "直取成功不得触发重搜"
    assert _wait_until(lambda: c.mpv.loadfile.call_count == 1, timeout=2.0), "后台切歌超时未执行 loadfile"
    c.mpv.loadfile.assert_called_once_with("http://x/direct.mp3")
    rows = c.db.get_history()
    assert len(rows) == 1 and rows[0]["source_type"] == "online"
    assert rows[0]["track_id"] == "old-1", "历史应记录直取 track 的 id"


def test_queue_online_song_researches_when_direct_fails(tmp_path):
    """直取失败（get_play_url 返回 None）→ 降级重搜兜底：按 title+artist 经
    OnlineResolver 拿新 track → URL → 播放。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()  # OnlineResolver 契约：resolve(query) -> {url, track, play_result}
    new_track = _mk_track()
    c.online_manager.get_play_url.return_value = None   # 直取失败
    c._resolver.resolve.return_value = {
        "url": "http://x/new.mp3",
        "track": new_track,
        "play_result": {"headers": None, "url": "http://x/new.mp3"},
    }
    c._play_song(_online_song())  # 存的 track_id 是 old-1
    # 重搜兜底在后台线程执行：先等 resolve 被调用，再断言 query
    assert _wait_until(lambda: c._resolver.resolve.called, timeout=2.0), "直取失败后未触发重搜兜底"
    assert c._resolver.resolve.call_args.args[0] == "夜曲 周杰伦"
    assert _wait_until(lambda: c.mpv.loadfile.call_count == 1, timeout=2.0), "后台切歌超时未执行 loadfile"
    c.mpv.loadfile.assert_called_once_with("http://x/new.mp3")
    rows = c.db.get_history()
    assert len(rows) == 1 and rows[0]["source_type"] == "online"
    assert rows[0]["track_id"] == "new-1", "历史应记录重搜拿到的新 track_id（非存的 old-1）"


def test_queue_online_song_not_found_skips(tmp_path):
    """直取失败且重搜无结果（resolver 返回 None）→ 主动跳到下一首（不卡；Timer 0.2s 后 next 到本地歌）。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    c.online_manager.get_play_url.return_value = None   # 直取失败
    c._resolver.resolve.return_value = None
    c.queue.add_all([
        _online_song(),
        {"id": 1, "title": "本地", "artist": "C", "path": "C:/l.mp3"},
    ])
    c.queue.play_at(0)
    c._play_song(c.queue.get_current())
    assert c.mpv.loadfile.call_count == 0          # 未播放
    time.sleep(0.6)                                # 等 _schedule_skip_online Timer(0.2)
    assert c.queue.get_current_index() == 1        # 已跳到下一首
    assert c.queue.get_current()["title"] == "本地"
    with patch("os.path.exists", return_value=True):
        c._play_song(c.queue.get_current())
    c.mpv.loadfile.assert_called_once_with("C:/l.mp3")


def test_queue_online_url_all_fail_skips(tmp_path):
    """直取失败且搜到候选但 URL 全拿不到（resolver 无结果）→ 同样主动跳过。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    c.online_manager.get_play_url.return_value = None   # 直取失败
    c._resolver.resolve.return_value = None
    c.queue.add_all([
        _online_song(),
        {"id": 2, "title": "下一首", "artist": "D", "path": "C:/l2.mp3"},
    ])
    c.queue.play_at(0)
    c._play_song(c.queue.get_current())
    assert c.mpv.loadfile.call_count == 0
    time.sleep(0.6)
    assert c.queue.get_current_index() == 1


def test_skip_does_not_hijack_user_switch(tmp_path):
    """跳过防覆盖：用户已切走（队列 current 变化）→ 不 next。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    c.online_manager.get_play_url.return_value = None   # 直取失败
    c._resolver.resolve.return_value = None
    c.queue.add_all([
        _online_song(),
        {"id": 1, "title": "本地", "artist": "C", "path": "C:/l.mp3"},
    ])
    c.queue.play_at(0)
    online = c.queue.get_current()
    c._play_song(online)
    c.queue.play_at(1)          # 用户手动切走
    c._do_skip_online(online)   # 延迟跳过触发 → 不应干预
    assert c.queue.get_current_index() == 1
    time.sleep(0.6)             # 原 Timer 也触发 → 幂等（is not song → return）


def test_queue_local_song_unaffected(tmp_path):
    """本地歌（music 表行，无 source_type）→ 原路径：直接 loadfile(path)、不查在线。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    song = {"id": 1, "title": "本地", "artist": "A", "path": "C:/l.mp3"}
    with patch("os.path.exists", return_value=True):
        c._play_song(song)
    c.mpv.loadfile.assert_called_once_with("C:/l.mp3")
    c._resolver.resolve.assert_not_called()


def test_queue_online_then_local_auto_next(tmp_path):
    """在线歌播放成功 → 播完 queue.next() 到下一首本地歌并正常播放。"""
    c = _make_ctrl(tmp_path)
    c.online_manager = MagicMock()
    c._resolver = MagicMock()
    c.online_manager.get_play_url.return_value = {
        "url": "http://x/direct.mp3", "headers": None}
    c.queue.add_all([
        _online_song(),
        {"id": 2, "title": "本地", "artist": "C", "path": "C:/l.mp3"},
    ])
    c.queue.play_at(0)
    c._play_song(c.queue.get_current())
    assert _wait_until(lambda: c.mpv.loadfile.call_count == 1, timeout=2.0), "后台切歌超时未执行 loadfile"
    assert c.mpv.loadfile.call_count == 1
    nxt = c.queue.next()
    assert nxt["title"] == "本地"
    with patch("os.path.exists", return_value=True):
        c._play_song(c.queue.get_current())
    c.mpv.loadfile.assert_called_with("C:/l.mp3")
