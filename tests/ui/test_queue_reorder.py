from PySide6.QtCore import QModelIndex, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
import pytest

from tests.ui.test_music_queue_view import setup_view, open_queue, wait_for


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    from app.ui.theme.manager import get_theme_manager
    get_theme_manager(app)
    return app


def test_actual_qt_mime_drop_reorders_business_queue_once_without_player_calls(setup_view):
    s = setup_view
    dialog = open_queue(s)
    model = dialog.other_model
    data = model.mimeData([model.index(2)])
    assert model.dropMimeData(data, Qt.MoveAction, 1, 0, QModelIndex())
    wait_for(lambda: [r["title"] for r in s.c.queue.snapshot()["items"]] == ["A", "C", "B"])
    wait_for(lambda: "已调整" in dialog.action_status.text())
    assert s.c._current_song is s.songs[0]
    assert not s.mpv.mock_calls


def test_snapshot_change_during_drag_rejects_old_request_and_current_cannot_drag(setup_view):
    s = setup_view
    dialog = open_queue(s)
    model = dialog.other_model
    assert not model.mimeData([model.index(0)]).hasFormat(model.MIME)
    data = model.mimeData([model.index(2)])
    s.c.queue.add_next(s.songs[3])
    wait_for(lambda: len(dialog.next_model.rows) == 1)
    assert model.dropMimeData(data, Qt.MoveAction, 1, 0, QModelIndex()) is False
    assert "已变化" in dialog.action_status.text()
    assert s.c.queue.next() is s.songs[3]


def test_shuffle_disables_drag_and_explains_reason_without_altering_priority(setup_view):
    s = setup_view
    dialog = open_queue(s)
    s.c.queue.add_next(s.songs[3])
    s.c.queue.set_play_mode("shuffle")
    wait_for(lambda: "随机" in dialog.mode_label.text())
    assert not dialog.other_list.dragEnabled()
    assert "随机" in dialog.other_list.toolTip()
    assert s.c.queue.next(auto=True) is s.songs[3]
