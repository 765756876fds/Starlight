"""播放历史：数据层 + 记录器 + 重播兜底。"""
import pytest

pytestmark = pytest.mark.unit


import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

from app.core.event_bus import Event, EventType


# ---------- 数据层 ----------

def _mkdb(tmp_path):
    from app.music.database import MusicDatabase
    return MusicDatabase(str(tmp_path / "h.db"))


def test_add_history_cap_200(tmp_path):
    """超 200 条删最旧：只留最近 200。"""
    db = _mkdb(tmp_path)
    try:
        for i in range(205):
            db.add_history(title=f"T{i}", artist="A", source_type="local",
                           music_id=i + 1, played_at=float(i))
        rows = db.get_history(limit=200)
        assert len(rows) == 200
        assert rows[0]["title"] == "T204"  # 最新在前
        titles = {r["title"] for r in rows}
        assert "T204" in titles
        assert "T0" not in titles and "T4" not in titles  # 最旧 5 条被删
    finally:
        db.close()


def test_get_history_desc(tmp_path):
    """按 played_at 倒序 + 字段完整。"""
    db = _mkdb(tmp_path)
    try:
        db.add_history(title="A", artist="a", source_type="local", music_id=1, played_at=100.0)
        db.add_history(title="B", artist="b", source_type="online",
                       source_name="kw", track_id="x", played_at=200.0)
        db.add_history(title="C", artist="c", source_type="local", music_id=2, played_at=150.0)
        rows = db.get_history()
        assert [r["title"] for r in rows] == ["B", "C", "A"]
        assert rows[1]["source_type"] == "local" and rows[1]["music_id"] == 2
        assert rows[0]["source_name"] == "kw" and rows[0]["track_id"] == "x"
        assert rows[0]["source_type"] == "online" and rows[0]["music_id"] == 0
    finally:
        db.close()


def test_clear_history(tmp_path):
    db = _mkdb(tmp_path)
    try:
        db.add_history(title="A", artist="a", source_type="local", music_id=1)
        db.clear_history()
        assert db.get_history() == []
    finally:
        db.close()


# ---------- 记录器 ----------

def _mk_recorder(tmp_path, bus):
    from app.music.history_recorder import HistoryRecorder
    from app.music.database import MusicDatabase
    db = MusicDatabase(str(tmp_path / "r.db"))
    rec = HistoryRecorder(db, bus)
    rec.start()
    return db, rec


def _ev(song):
    return Event(event_type=EventType.MUSIC_TRACK_CHANGED,
                 data={"song": song}, source="music")


def test_recorder_event_subscription_records(tmp_path, event_bus):
    """事件订阅 + 记录：publish MUSIC_TRACK_CHANGED → 历史多一条。"""
    db, rec = _mk_recorder(tmp_path, event_bus)
    try:
        event_bus.publish(_ev({"id": 7, "title": "晴天", "artist": "周杰伦",
                               "path": "/x/1.mp3"}))
        rows = db.get_history()
        assert len(rows) == 1
        assert rows[0]["source_type"] == "local"
        assert rows[0]["music_id"] == 7
        assert rows[0]["title"] == "晴天"
        # 幂等 start：再 start 不重复订阅
        rec.start()
        event_bus.publish(_ev({"id": 7, "title": "晴天", "artist": "周杰伦",
                               "path": "/x/1.mp3"}))
        assert len(db.get_history()) == 1  # 去重生效
    finally:
        db.close()
        rec.stop()


def test_recorder_online_fields(tmp_path, event_bus):
    """在线歌：source_type=online + source_name/track_id 落库。"""
    db, rec = _mk_recorder(tmp_path, event_bus)
    try:
        event_bus.publish(_ev({"id": -1, "title": "K", "artist": "V",
                               "source": "online", "source_name": "kw",
                               "track_id": "abc"}))
        rows = db.get_history()
        assert len(rows) == 1
        assert rows[0]["source_type"] == "online"
        assert rows[0]["source_name"] == "kw"
        assert rows[0]["track_id"] == "abc"
        assert rows[0]["music_id"] == 0
    finally:
        db.close()
        rec.stop()


def test_recorder_dedup_5min(tmp_path):
    """5 分钟内同歌（title+artist+source_type）不重复记；不同歌新记。"""
    db, rec = _mk_recorder(tmp_path, MagicMock())
    try:
        s = {"id": 1, "title": "同一首", "artist": "A", "path": "/x/a.mp3"}
        rec._on_track_changed(_ev(s))
        rec._on_track_changed(_ev(s))  # 同歌 → 去重
        assert len(db.get_history()) == 1
        rec._on_track_changed(_ev({"id": 2, "title": "同一首", "artist": "A",
                                   "path": "/x/b.mp3"}))  # 同歌不同 id → 仍去重
        assert len(db.get_history()) == 1
        rec._on_track_changed(_ev({"id": 3, "title": "另一首", "artist": "A",
                                   "path": "/x/c.mp3"}))
        assert len(db.get_history()) == 2
    finally:
        db.close()


def test_recorder_invalid_local_id_skipped(tmp_path):
    """本地 id 无效（<=0 且非在线）→ 不记（无法重播的脏数据）。"""
    db, rec = _mk_recorder(tmp_path, MagicMock())
    try:
        rec._on_track_changed(_ev({"id": 0, "title": "脏数据", "artist": "",
                                   "path": ""}))
        assert db.get_history() == []
    finally:
        db.close()


# ---------- controller 薄方法 / 重播兜底 ----------

def _make_ctrl(tmp_path):
    from app.music.controller import MusicController
    cfg = {
        "database": str(tmp_path / "t.db"),
        "scan_dirs": [],
        "extensions": [".mp3"],
        "mpv_path": "mpv",
        "mpv_ipc_pipe": "\\\\.\\pipe\\test_p1",
        "default_volume": 70,
    }
    with patch("app.music.controller.MpvController"), \
         patch("app.music.controller.LxMusicClient") as lx_cls:
        lx_cls.return_value.is_connected.return_value = False
        c = MusicController(cfg)
    c.mpv = MagicMock()
    # mpv.py loadfile 契约：成功返回 {"error": "success"}（控制器据此收口播放成功）
    c.mpv.loadfile.return_value = {"error": "success"}
    return c


def test_play_history_item_local_missing(tmp_path):
    """本地重播：music_id 找不到 → False，不崩。"""
    c = _make_ctrl(tmp_path)
    assert c.play_history_item({"music_id": 99999, "title": "无", "artist": "x"}) is False


def test_play_history_item_online_not_found(tmp_path):
    """在线重播：搜不到 → False，不崩。"""
    c = _make_ctrl(tmp_path)
    c.search = MagicMock()
    c.search.search.return_value = []
    c.online_manager = MagicMock()
    c.online_manager.search_all.return_value = {}
    assert c.play_history_item({"music_id": 0, "title": "搜不到", "artist": "y"}) is False


def test_play_history_item_local_ok(tmp_path):
    """本地重播：找到歌 → play_song 且返回 True。"""
    c = _make_ctrl(tmp_path)
    song = {"id": 5, "path": "C:/fake/ok.mp3", "title": "T", "artist": "A"}
    c.db.get_song_by_id = MagicMock(return_value=song)
    with patch("os.path.exists", return_value=True):
        with patch.object(c, "play_song") as ps:
            ok = c.play_history_item({"music_id": 5, "title": "T", "artist": "A"})
    assert ok is True
    ps.assert_called_once_with(song)


# ---------- play_online 直接记历史（不走事件） ----------

def _mk_online_track(name="T", artist="A", source="kw", tid="1"):
    from app.music.online.models import OnlineTrack
    return OnlineTrack(source_name=source, track_id=tid, title=name, artist=artist)


def _ctrl_with_history(tmp_path, event_bus):
    """把 controller 的 recorder 换成连到测试 event_bus 的实例。"""
    c = _make_ctrl(tmp_path)
    from app.music.history_recorder import HistoryRecorder
    rec = HistoryRecorder(c.db, event_bus)
    rec.start()
    c.history_recorder = rec
    c.online_manager = MagicMock()
    return c, rec


def test_play_online_success_records(tmp_path, event_bus):
    """play_online 成功 → 历史多一条（online + source_name/track_id）。"""
    c, rec = _ctrl_with_history(tmp_path, event_bus)
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False
    ok = c.play_online(_mk_online_track())
    assert ok is True
    rows = c.db.get_history()
    assert len(rows) == 1
    assert rows[0]["source_type"] == "online"
    assert rows[0]["source_name"] == "kw"
    assert rows[0]["track_id"] == "1"
    assert rows[0]["title"] == "T"


def test_play_online_fail_not_recorded(tmp_path, event_bus):
    """play_online 失败（无 URL）→ 不记历史。"""
    c, rec = _ctrl_with_history(tmp_path, event_bus)
    c.online_manager.get_play_url.return_value = None
    ok = c.play_online(_mk_online_track())
    assert ok is False
    assert c.db.get_history() == []


def test_play_online_shared_dedup_with_event(tmp_path, event_bus):
    """共用去重：事件路径先记在线同歌，5 分钟内 play_online 同歌 → 不重复；不同歌新记。"""
    c, rec = _ctrl_with_history(tmp_path, event_bus)
    event_bus.publish(Event(
        event_type=EventType.MUSIC_TRACK_CHANGED,
        data={"song": {"id": -1, "title": "T", "artist": "A", "source": "online",
                       "source_name": "kw", "track_id": "1"}},
        source="music"))
    assert len(c.db.get_history()) == 1
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False
    ok = c.play_online(_mk_online_track())
    assert ok is True
    assert len(c.db.get_history()) == 1  # 去重拦住
    c.play_online(_mk_online_track(name="U", tid="2"))
    assert len(c.db.get_history()) == 2


def test_play_online_does_not_trigger_track_changed(tmp_path, event_bus):
    """play_online 不发 MUSIC_TRACK_CHANGED（语音链路订阅者不受影响）。"""
    c, rec = _ctrl_with_history(tmp_path, event_bus)
    fired = []
    event_bus.subscribe(EventType.MUSIC_TRACK_CHANGED,
                        lambda ev: fired.append(ev))
    c.online_manager.get_play_url.return_value = {"url": "http://x/a.mp3", "headers": None}
    c.mpv.get_property.return_value = False
    ok = c.play_online(_mk_online_track())
    assert ok is True
    assert fired == []          # 事件未被触发
    assert len(c.db.get_history()) == 1  # 但历史仍记录
