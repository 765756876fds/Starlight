"""歌单 UI：第 4 tab 切 tab 刷新 + 右键菜单（在线隐藏本地项 + 删除二次确认）。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from unittest.mock import MagicMock, patch

import pytest
pytestmark = pytest.mark.ui



@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


class FakeMusic:
    """面板所需最小 controller 接口（含歌单）。"""

    def __init__(self):
        self.playlists = [{"id": 1, "name": "P1", "song_count": 1}]
        self.songs = [{"id": 9, "playlist_id": 1, "title": "S1", "artist": "A1",
                       "source_type": "local", "source_name": "", "track_id": "",
                       "music_id": 3, "added_at": 1.0}]
        self.song_paths = {}

    def get_playlists(self):
        return self.playlists

    def get_playlist_songs(self, pid):
        return self.songs

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_volume(self):
        return 70

    def set_volume(self, v):
        self._volume = v

    def get_online_sources(self):
        return []

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return None

    def get_history(self, limit=200):
        return []

    def clear_history(self):
        pass

    def play_history_item(self, item):
        return True

    def play_playlist_item(self, item):
        return True

    def add_next(self, song):
        pass

    def add_to_playlist(self, pid, song):
        return True

    def create_playlist(self, name):
        return 1

    def rename_playlist(self, pid, name):
        return True

    def delete_playlist(self, pid):
        pass

    def remove_from_playlist(self, pid, sid):
        return True

    def get_song_path(self, mid):
        return self.song_paths.get(mid, "")

    def delete_local_file(self, song):
        return True, ""


def test_switch_to_playlist_tab_refreshes(qapp):
    """切到歌单 tab（index=3）→ 刷新歌单列表；其他 tab 不触发。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    with patch.object(m, "get_playlists", wraps=m.get_playlists) as gp:
        panel = MusicPanel(music=m)
        assert gp.call_count == 0     # 构造不刷（懒，切到才刷）
        panel.tabs.setCurrentIndex(3)
        assert gp.call_count == 1
        panel.tabs.setCurrentIndex(0)
        panel.tabs.setCurrentIndex(1)
        panel.tabs.setCurrentIndex(2)
        assert gp.call_count == 1     # 本地/在线/历史不触发歌单刷新


def test_playlist_selection_renders_songs(qapp):
    """选中歌单 → 右侧渲染歌单内歌曲。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    panel.tabs.setCurrentIndex(3)
    assert panel.list_playlists.count() == 1
    panel.list_playlists.setCurrentRow(0)
    assert panel.list_playlist_songs.count() == 1
    assert "S1" in panel.list_playlist_songs.item(0).text()
    assert "本地" in panel.list_playlist_songs.item(0).text()


def test_online_song_menu_hides_local_actions(qapp):
    """在线歌右键：不显示「打开所在文件夹 / 删除本地文件」。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    song = {"title": "O", "artist": "B", "source_type": "online",
            "source_name": "kw", "track_id": "x", "music_id": 0}
    menu = panel._build_song_menu(song)
    top = [a.text() for a in menu.actions()]
    assert "下一首播放" in top
    assert any(a.text() == "添加到歌单 ▸" for a in menu.actions())
    assert "打开所在文件夹" not in top
    assert "删除本地文件" not in top


def test_local_song_menu_has_folder_and_delete(qapp, tmp_path):
    """本地歌右键：显示「打开所在文件夹 / 删除本地文件」。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    song = {"title": "L", "artist": "C", "source_type": "local",
            "source_name": "", "track_id": "", "music_id": 5, "path": str(f)}
    menu = panel._build_song_menu(song)
    top = [a.text() for a in menu.actions()]
    assert "打开所在文件夹" in top
    assert "删除本地文件" in top


def test_playlist_song_menu_has_remove(qapp):
    """歌单内歌曲右键：多「从歌单内删除」。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    song = {"title": "L", "artist": "C", "source_type": "local",
            "source_name": "", "track_id": "", "music_id": 5}
    menu = panel._build_song_menu(song, playlist_song_id=9)
    top = [a.text() for a in menu.actions()]
    assert "从歌单内删除" in top


def test_delete_local_file_cancel_no_call(qapp):
    """删除本地文件：确认框默认取消 → 不调用 controller。"""
    from PySide6.QtWidgets import QMessageBox
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    song = {"title": "L", "source_type": "local", "music_id": 5, "path": "C:/x/a.mp3"}
    with patch.object(m, "delete_local_file", wraps=m.delete_local_file) as dl, \
         patch("app.ui.panels.music_panel.QMessageBox.warning",
               return_value=QMessageBox.StandardButton.Cancel):
        panel._delete_local_file(song)
    dl.assert_not_called()


def test_delete_local_file_ok_calls_controller(qapp):
    """确认（Ok）→ controller.delete_local_file 被调 + 刷新本地列表。"""
    from PySide6.QtWidgets import QMessageBox
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    m.delete_local_file = MagicMock(return_value=(True, ""))
    panel = MusicPanel(music=m)
    panel._local_songs = []
    song = {"title": "L", "source_type": "local", "music_id": 5, "path": "C:/x/a.mp3"}
    with patch("app.ui.panels.music_panel.QMessageBox.warning",
               return_value=QMessageBox.StandardButton.Ok):
        panel._delete_local_file(song)
    m.delete_local_file.assert_called_once_with(song)


# ---------- 实机 4 bug 回归 ----------

def test_add_to_playlist_normalizes_local_row(qapp):
    """回归 Bug1+2：本地列表项（原始 dict，无 source_type 键）加歌单 →
    规范化后 source_type='local'、music_id=row.id，不被默认成 online。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    m.add_to_playlist = MagicMock(return_value=True)
    panel = MusicPanel(music=m)
    local_row = {"id": 7, "title": "本地歌", "artist": "A", "path": "C:/x/7.mp3"}
    panel._add_to_playlist(1, local_row)
    got = m.add_to_playlist.call_args.args[1]
    assert got["source_type"] == "local"
    assert got["music_id"] == 7
    assert got["title"] == "本地歌"


def test_add_to_playlist_two_different_local_songs(qapp):
    """回归 Bug1：两首不同本地歌加同一歌单都成功（db 默认 local + 规范化）。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    results = []
    m.add_to_playlist = MagicMock(side_effect=lambda pid, s: results.append(s) or True)
    panel._add_to_playlist(1, {"id": 1, "title": "T1", "artist": "A1"})
    panel._add_to_playlist(1, {"id": 2, "title": "T2", "artist": "A2"})
    assert m.add_to_playlist.call_count == 2
    assert [r["music_id"] for r in results] == [1, 2]


def test_on_next_play_local_sets_path(qapp):
    """回归 Bug3：本地歌右键下一首播放 → 插入队列的 dict 带 path（补自 get_song_path）。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    m.song_paths = {5: "C:/x/5.mp3"}
    panel = MusicPanel(music=m)
    with patch.object(m, "add_next", wraps=m.add_next) as an:
        panel._on_next_play({"music_id": 5, "title": "L", "source_type": "local"})
    an.assert_called_once()
    d = an.call_args.args[0]
    assert d["path"] == "C:/x/5.mp3"
    assert d["id"] == 5


def test_on_next_play_local_missing_path_no_insert(qapp):
    """回归 Bug3 兜底：本地歌 path 取不到 → 状态栏提示、不插队。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()  # song_paths 空 → get_song_path 返回 ""
    panel = MusicPanel(music=m)
    with patch.object(m, "add_next", wraps=m.add_next) as an:
        panel._on_next_play({"music_id": 999, "title": "没了", "source_type": "local"})
    an.assert_not_called()
    assert panel.lbl_status.text() == "该歌已不可用"


def test_open_song_folder_quotes_path(qapp):
    """回归 Bug4：explorer 只吃 cmd shell 形态（list 模式标准 argv 解析会失效
    → 打开"文档"默认位置）；/select,"<path>" 整串 + shell=True。"""
    from app.ui.panels.music_panel import MusicPanel
    m = FakeMusic()
    panel = MusicPanel(music=m)
    with patch("subprocess.Popen") as popen:
        panel._open_song_folder("C:/x/a b.mp3")
    args, kw = popen.call_args
    assert kw.get("shell") is True
    assert args[0] == 'explorer /select,"C:\\x\\a b.mp3"'


@pytest.fixture
def next_play_view(qapp):
    """Own the panel and its independent volume popup through Qt teardown."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from app.ui.panels.music_panel import MusicPanel
    music = FakeMusic()
    panel = MusicPanel(music=music)
    yield panel, music
    panel._progress_polling = False
    panel._timer.stop()
    thread = getattr(panel, "_progress_thread", None)
    if thread is not None:
        thread.join(timeout=1.)
    popup = panel.btn_volume._popover
    popup.removeEventFilter(panel.btn_volume)
    popup.close()
    popup.deleteLater()
    panel.close()
    panel.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    qapp.processEvents()


@pytest.mark.parametrize("result,message", [
    (True, "已插到下一首：D"),
    (False, "加入下一首失败，请重试"),
    (None, "加入下一首结果未确认"),
    (1, "加入下一首结果未确认"),
    ({"error": "success"}, "加入下一首结果未确认"),
])
def test_next_play_feedback_requires_explicit_business_success(next_play_view, result, message):
    panel, music = next_play_view
    music.add_next = MagicMock(return_value=result)
    panel._on_next_play({"title": "D", "source_type": "online", "track_id": "D"})
    music.add_next.assert_called_once()
    assert panel.lbl_status.text() == message


def test_next_play_exception_replaces_stale_success_with_failure(next_play_view):
    panel, music = next_play_view
    panel.lbl_status.setText("已插到下一首：旧歌")
    music.add_next = MagicMock(side_effect=RuntimeError("isolated failure"))
    panel._on_next_play({"title": "D", "source_type": "online"})
    assert panel.lbl_status.text() == "加入下一首失败，请稍后重试"
    music.add_next.assert_called_once()


def test_next_play_invalid_metadata_does_not_escape_qt_slot(next_play_view):
    panel, music = next_play_view
    music.add_next = MagicMock(return_value=True)
    panel._on_next_play({"title": "D", "source_type": "local", "music_id": "invalid"})
    music.add_next.assert_not_called()
    assert panel.lbl_status.text() == "加入下一首失败，请稍后重试"


def test_next_play_menu_signal_inserts_only_once(next_play_view, qapp):
    panel, music = next_play_view
    music.add_next = MagicMock(return_value=True)
    menu = panel._build_song_menu({"title": "D", "source_type": "online", "track_id": "D"})
    action = next(a for a in menu.actions() if a.text() == "下一首播放")
    action.trigger()
    qapp.processEvents()
    music.add_next.assert_called_once()
    assert panel.lbl_status.text() == "已插到下一首：D"


def test_next_play_request_and_current_song_survive_page_switch(next_play_view, qapp):
    from PySide6.QtWidgets import QTabWidget, QWidget
    from app.music.controller import MusicController
    from app.music.queue import PlayMode, PlayQueue
    panel, music = next_play_view
    controller = MusicController.__new__(MusicController)
    controller.queue = PlayQueue()
    controller.queue.add_all([{"title": s} for s in "ABC"])
    controller.queue.play_at(0)
    controller.queue.set_play_mode(PlayMode.SHUFFLE)
    music.add_next = controller.add_next
    music.get_current_song = controller.queue.get_current
    music.get_play_mode = controller.queue.get_play_mode
    panel._on_next_play({"title": "D", "source_type": "online", "track_id": "D"})
    outer = QTabWidget()
    outer.addTab(panel, "音乐")
    outer.addTab(QWidget(), "其他")
    outer.show()
    qapp.processEvents()
    try:
        outer.setCurrentIndex(1)
        qapp.processEvents()
        outer.setCurrentIndex(0)
        panel.tabs.setCurrentIndex(1)
        panel.tabs.setCurrentIndex(0)
        qapp.processEvents()
        assert panel._music is music
        assert controller.queue.get_current()["title"] == "A"
        assert controller.queue.next()["title"] == "D"
        assert controller.queue.size() == 4
    finally:
        outer.removeTab(0)
        panel.setParent(None)
        outer.close()
        outer.deleteLater()
