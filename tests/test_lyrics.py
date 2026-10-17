"""批 3a 桌面歌词基础版测试：LRC 解析 + 本地 .lrc 读取 + 歌词浮窗同步。"""
import os
from unittest.mock import MagicMock

import pytest

from app.music.lyrics import parse_lrc, find_local_lrc, load_lyrics_from_file
from app.music.online.models import OnlineTrack


# ---------- LRC 解析 ----------
class TestParseLrc:
    def test_standard_format(self):
        lines = parse_lrc("[00:01.00]第一句\n[00:02.50]第二句")
        assert lines == [(1.0, "第一句"), (2.5, "第二句")]

    def test_millisecond_precision(self):
        lines = parse_lrc("[00:01.123]毫秒行\n[00:02.450]另一行")
        assert lines[0][0] == pytest.approx(1.123)
        assert lines[1][0] == pytest.approx(2.45)

    def test_meta_tags_ignored(self):
        lines = parse_lrc(
            "[ti:晴天]\n[ar:周杰伦]\n[al:叶惠美]\n[by:]\n[offset:0]\n[00:01.00]歌词"
        )
        assert lines == [(1.0, "歌词")]

    def test_empty_lines_and_no_timestamp_filtered(self):
        lines = parse_lrc("\n\n纯文本无时间轴\n[00:01.00]有效\n \n")
        assert lines == [(1.0, "有效")]

    def test_multiple_timestamps_on_one_line(self):
        lines = parse_lrc("[00:01.00][00:03.00]同一句")
        assert lines == [(1.0, "同一句"), (3.0, "同一句")]

    def test_offset_applied(self):
        lines = parse_lrc("[offset:500]\n[00:01.00]偏移后")
        assert lines[0][0] == pytest.approx(1.5)

    def test_sorted_by_time(self):
        lines = parse_lrc("[00:03.00]后\n[00:01.00]前")
        assert [t for t, _ in lines] == [1.0, 3.0]

    def test_minutes_field(self):
        lines = parse_lrc("[01:30.00]一分半")
        assert lines[0][0] == pytest.approx(90.0)

    def test_empty_text_returns_empty(self):
        assert parse_lrc("") == []
        assert parse_lrc(None) == []


# ---------- 本地 .lrc 查找 ----------
class TestFindLocalLrc:
    def test_same_name_match(self, tmp_path):
        mp3 = tmp_path / "晴天 - 周杰伦.mp3"
        mp3.write_bytes(b"\xff\xfb\x00\x00")  # 假 mp3 头
        lrc = tmp_path / "晴天 - 周杰伦.lrc"
        lrc.write_text("[00:01.00]词", encoding="utf-8")
        found = find_local_lrc(str(mp3))
        assert found == str(lrc)

    def test_no_lrc_returns_none(self, tmp_path):
        mp3 = tmp_path / "no_lrc.mp3"
        mp3.write_bytes(b"\xff\xfb\x00\x00")
        assert find_local_lrc(str(mp3)) is None

    def test_none_path_returns_none(self):
        assert find_local_lrc(None) is None


class TestLoadLyricsFromFile:
    def test_utf8_file(self, tmp_path):
        p = tmp_path / "a.lrc"
        p.write_text("[00:01.00]你好世界", encoding="utf-8")
        assert load_lyrics_from_file(str(p)) == [(1.0, "你好世界")]

    def test_gbk_file_fallback(self, tmp_path):
        p = tmp_path / "b.lrc"
        p.write_bytes("[00:01.00]中文歌词".encode("gbk"))
        lines = load_lyrics_from_file(str(p))
        assert lines == [(1.0, "中文歌词")]

    def test_missing_file_returns_empty(self):
        assert load_lyrics_from_file("Z:/definitely/missing.lrc") == []


# ---------- 歌词浮窗 ----------
@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _controller(mpv_pos=10.0, current_song=None, online_manager=None):
    """构造 mock 控制器（对齐项目 MagicMock 惯例）。"""
    ctrl = MagicMock()
    ctrl.mpv = MagicMock()
    ctrl.mpv.get_live_playback_time.return_value = mpv_pos
    ctrl.get_current_song.return_value = current_song
    ctrl.online_manager = online_manager
    ctrl.event_bus = MagicMock()
    return ctrl


class TestLyricsWindow:
    def test_construct_and_set_lyrics(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        w = LyricsWindow(_controller())
        w.set_lyrics([(1.0, "a"), (2.0, "b")], "标题")
        assert len(w._lines) == 2
        assert w._title == "标题"
        assert w._current_idx == 0

    def test_match_index(self):
        from app.ui.widgets.lyrics_window import LyricsWindow
        lines = [(1.0, "a"), (2.0, "b"), (5.0, "c")]
        assert LyricsWindow.match_index(lines, 0.5) == 0   # 首行前 → 0
        assert LyricsWindow.match_index(lines, 1.0) == 0
        assert LyricsWindow.match_index(lines, 2.5) == 1
        assert LyricsWindow.match_index(lines, 10.0) == 2  # 末行后 → 最后一行
        assert LyricsWindow.match_index([], 5.0) == -1

    def test_sync_updates_current_index(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        ctrl = _controller(mpv_pos=3.0)
        w = LyricsWindow(ctrl)
        w.set_lyrics([(1.0, "a"), (2.0, "b"), (4.0, "c")])
        w._sync()
        assert w._current_idx == 1

    def test_sync_ignores_exception_and_none(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        ctrl = _controller()
        ctrl.mpv.get_live_playback_time.side_effect = RuntimeError("boom")
        w = LyricsWindow(ctrl)
        w.set_lyrics([(1.0, "a")])
        w._sync()  # 不崩

        ctrl.mpv.get_live_playback_time.side_effect = None
        ctrl.mpv.get_live_playback_time.return_value = None
        w._sync()  # None 不更新
        assert w._current_idx == 0

    def test_track_changed_local_lrc(self, qapp, tmp_path):
        from app.core.event_bus import Event, EventType
        from app.ui.widgets.lyrics_window import LyricsWindow
        mp3 = tmp_path / "本地歌.mp3"
        mp3.write_bytes(b"\xff\xfb\x00\x00")
        (tmp_path / "本地歌.lrc").write_text("[00:01.00]本地歌词", encoding="utf-8")
        ctrl = _controller(current_song={"id": 1, "title": "本地歌", "path": str(mp3)})
        w = LyricsWindow(ctrl)
        w._on_track_changed(Event(EventType.MUSIC_TRACK_CHANGED,
                                  data={"song": {"id": 1, "title": "本地歌", "path": str(mp3)}}))
        assert len(w._lines) == 1
        assert w._lines[0][1] == "本地歌词"

    def test_track_changed_online_get_lyric(self, qapp):
        from app.core.event_bus import Event, EventType
        from app.ui.widgets.lyrics_window import LyricsWindow
        track = OnlineTrack(source_name="tx", track_id="97773",
                            title="晴天", artist="周杰伦")
        mgr = MagicMock()
        mgr.get_lyric.return_value = "[00:01.00]在线歌词"
        ctrl = _controller(current_song=track, online_manager=mgr)
        w = LyricsWindow(ctrl)
        w._on_track_changed(Event(EventType.MUSIC_TRACK_CHANGED, data={"song": track}))
        assert len(w._lines) == 1
        assert w._lines[0][1] == "在线歌词"

    def test_no_lyrics_placeholder(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        ctrl = _controller(current_song={"id": 1, "title": "无词", "path": "Z:/missing.mp3"})
        w = LyricsWindow(ctrl)
        w._load_current()
        assert w._lines == []          # 无歌词 → 空 → paint 画占位
        assert w._title == "无词"

    def test_paint_does_not_crash(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        w = LyricsWindow(_controller())
        w.set_lyrics([(1.0, "第一句歌词"), (2.0, "第二句歌词")], "歌")
        w._current_idx = 1
        w.resize(640, 108)
        # 直接调 paintEvent（fake QPaintEvent）——防"只在真实渲染才执行"漏测
        from PySide6.QtGui import QPaintEvent
        w.paintEvent(QPaintEvent(w.rect()))
        # 占位渲染也不崩
        w.set_lyrics([], "")
        w.paintEvent(QPaintEvent(w.rect()))

    def test_start_stop_subscribes(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        ctrl = _controller()
        w = LyricsWindow(ctrl)
        w.start()
        ctrl.event_bus.subscribe.assert_called()
        w.stop()
        ctrl.event_bus.unsubscribe.assert_called()

    def test_row_cache_reused(self, qapp):
        from app.ui.widgets.lyrics_window import LyricsWindow
        w = LyricsWindow(_controller())
        pm1 = w._row_pixmap("同一句", True)
        pm2 = w._row_pixmap("同一句", True)
        assert pm1 is pm2  # 缓存命中（同文本同态复用）


# ---------- 音乐面板歌词开关 ----------
class TestMusicPanelLyricsToggle:
    def test_toggle_creates_and_hides_window(self, qapp):
        from app.ui.panels.music_panel import MusicPanel
        fake = MagicMock()
        fake.get_volume.return_value = 50
        fake.get_online_sources.return_value = []
        p = MusicPanel(music=fake)
        assert p._lyrics_window is None
        p._on_toggle_lyrics(True)
        assert p._lyrics_window is not None
        p._on_toggle_lyrics(False)
        assert p._lyrics_window is not None  # 保留实例，可再开


# ---------- manager.get_lyric 路由（在线歌断点修复） ----------
class TestManagerGetLyricRouting:
    def _track(self, source_name="酷狗音乐"):
        return OnlineTrack(source_name=source_name, track_id="20505418",
                           title="晴天", artist="周杰伦")

    def test_direct_hit_returns_lyric(self):
        from app.music.online.manager import OnlineSourceManager
        src = MagicMock()
        src.name = "tx"
        src.get_lyric.return_value = "[00:01.00]直接命中"
        mgr = OnlineSourceManager()
        mgr.register(src)
        assert mgr.get_lyric(self._track("tx")) == "[00:01.00]直接命中"

    def test_direct_miss_accepts_hit_routes(self):
        from app.music.online.manager import OnlineSourceManager
        # js_bridge 风格源：注册名 "js_bridge"，accepts 命中内部插件名
        bridge = MagicMock()
        bridge.name = "js_bridge"
        bridge.accepts.return_value = True
        bridge.get_lyric.return_value = "[00:01.00]回退命中"
        mgr = OnlineSourceManager()
        mgr.register(bridge)
        # track.source_name 是插件名（"酷狗音乐"）→ 定向 miss → accepts 命中
        assert mgr.get_lyric(self._track("酷狗音乐")) == "[00:01.00]回退命中"
        bridge.accepts.assert_called_once()
        bridge.get_lyric.assert_called_once()

    def test_all_miss_returns_none(self):
        from app.music.online.manager import OnlineSourceManager
        http_src = MagicMock()
        http_src.name = "http_src"
        http_src.accepts.return_value = False   # 不接受该 track
        mgr = OnlineSourceManager()
        mgr.register(http_src)
        assert mgr.get_lyric(self._track("酷狗音乐")) is None
        http_src.get_lyric.assert_not_called()  # 未命中不调（不对 http 源发无谓请求）

    def test_single_source_failure_continues(self):
        from app.music.online.manager import OnlineSourceManager
        bad = MagicMock()
        bad.name = "js_bridge"
        bad.accepts.return_value = True
        bad.get_lyric.side_effect = RuntimeError("boom")
        good = MagicMock()
        good.name = "other"
        good.accepts.return_value = True
        good.get_lyric.return_value = "[00:01.00]下一个源"
        mgr = OnlineSourceManager()
        mgr.register(bad)
        mgr.register(good)
        assert mgr.get_lyric(self._track("酷狗音乐")) == "[00:01.00]下一个源"
