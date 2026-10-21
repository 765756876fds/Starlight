"""
历史/歌单封面补全（数据流）测试：
- controller.get_history / get_playlist_songs：本地行反查 music 表补 path（ID3/同目录封面可读）；
  在线行从最近封面缓存补 cover_url
- remember_cover 登记（播放/在线搜索/榜单）→ 历史/歌单缺字段时命中
- MusicPanel._refresh_history 日志打印前 3 个 item 字段，且 UserRole 携带补全后的 path/cover_url
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
pytestmark = pytest.mark.ui


from app.music.controller import MusicController


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeDb:
    """controller._enrich_cover 所需的最小 db 接口。"""

    def __init__(self):
        self.by_id = {7: {"id": 7, "path": r"C:\music\a.flac", "title": "夜曲", "artist": "周杰伦"}}
        self.by_search = {"夜曲": {"id": 7, "path": r"C:\music\a.flac", "title": "夜曲", "artist": "周杰伦"}}

    def get_song_by_id(self, song_id):
        return self.by_id.get(int(song_id))

    def search(self, query, limit=5):
        for k, v in self.by_search.items():
            if k in query or query in k:
                return [v]
        return []


@pytest.fixture()
def ctrl():
    c = MusicController.__new__(MusicController)
    c.db = FakeDb()
    c._cover_mem = {}
    return c


def test_history_local_row_enriched_with_path(ctrl):
    """本地历史行（仅 title/music_id）→ 反查 music 表补 path。"""
    row = {"title": "夜曲", "artist": "周杰伦", "source_type": "local",
           "music_id": 7, "played_at": 100.0}
    out = ctrl._enrich_cover(dict(row))
    assert out.get("path") == r"C:\music\a.flac"
    # 原行不被修改（lookup_cover 语义）
    assert row.get("path") is None


def test_history_local_row_title_only_lookup(ctrl):
    """无 music_id 时按 title 反查补 path。"""
    row = {"title": "夜曲", "artist": "", "source_type": "local", "music_id": 0}
    out = ctrl._enrich_cover(dict(row))
    assert out.get("path") == r"C:\music\a.flac"


def test_online_row_enriched_from_recent_cache(ctrl):
    """在线历史行 → 从最近封面缓存补 cover_url（remember_cover 登记后）。"""
    ctrl.remember_cover(title="晴天", artist="周杰伦", source_name="酷我音乐",
                        track_id="k1", cover_url="https://img/cover.jpg")
    row = {"title": "晴天", "artist": "周杰伦", "source_type": "online",
           "source_name": "酷我音乐", "track_id": "k1", "played_at": 200.0}
    out = ctrl._enrich_cover(dict(row))
    assert out.get("cover_url") == "https://img/cover.jpg"


def test_online_row_by_track_id_cache(ctrl):
    """缓存命中 source|track_id 键（标题不匹配时）。"""
    ctrl.remember_cover(title="", artist="", source_name="网易云", track_id="n9",
                        cover_url="https://img/n9.jpg")
    row = {"title": "某歌", "artist": "", "source_type": "online",
           "source_name": "网易云", "track_id": "n9"}
    out = ctrl._enrich_cover(dict(row))
    assert out.get("cover_url") == "https://img/n9.jpg"


def test_existing_cover_not_overwritten(ctrl):
    """已有 cover_url/path 的行直接返回，不重复查询。"""
    row = {"title": "x", "artist": "", "source_type": "local",
           "music_id": 7, "cover_url": "https://img/x.jpg"}
    out = ctrl._enrich_cover(dict(row))
    assert out.get("cover_url") == "https://img/x.jpg"


def test_get_history_and_playlist_return_enriched(ctrl):
    """get_history / get_playlist_songs 返回补全后的行。"""
    rows = [{"title": "夜曲", "artist": "周杰伦", "source_type": "local",
             "music_id": 7, "played_at": 1.0}]
    ctrl.db.get_history = lambda limit=200: [dict(r) for r in rows]
    out = ctrl.get_history(limit=10)
    assert out[0].get("path") == r"C:\music\a.flac"

    ctrl.db.get_playlist_songs = lambda pid: [dict(r) for r in rows]
    out2 = ctrl.get_playlist_songs(1)
    assert out2[0].get("path") == r"C:\music\a.flac"


def test_music_panel_history_logs_and_carries_cover(qapp, capsys):
    """面板 _refresh_history：前 3 个 item 打印 title/music_id/cover_url/path 日志，
    UserRole 携带补全后的 path（Delegate 可读本地封面）。"""
    from PySide6.QtCore import Qt
    from app.ui.panels.music_panel import MusicPanel

    class FakeMusic:
        def get_play_mode(self):
            return "sequential"

        def is_highlight_mode(self):
            return False

        def get_volume(self):
            return 70

        def get_online_sources(self):
            return []

        def search_local(self, kw=""):
            return []

        def get_current_song(self):
            return None

        def get_history(self, limit=200):
            return [{"title": "夜曲", "artist": "周杰伦", "source_type": "local",
                     "music_id": 7, "played_at": 100.0}]

        def lookup_cover(self, row):
            row["path"] = r"C:\music\a.flac"
            return row

    panel = MusicPanel(music=FakeMusic())
    try:
        panel._refresh_history()
        item = panel.list_history.item(0)
        data = item.data(Qt.UserRole)
        assert data.get("path") == r"C:\music\a.flac", "历史卡片必须携带补全后的 path"
        assert "夜曲" in data.get("title", "")
        out = capsys.readouterr().out
        assert "[MusicPanel] history item#0" in out, "必须打印前 3 个 item 字段日志"
        assert "path=" in out
    finally:
        panel._timer.stop()
