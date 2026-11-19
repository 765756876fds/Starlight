"""Caption drawing with real Qt, and the result rows' existing playback data."""
import pytest
from PySide6.QtCore import QRect, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QListWidget, QListWidgetItem, QStyleOptionViewItem

from app.ui.panels.movie_panel import _PosterWallDelegate, _make_poster_placeholder
from app.ui.theme.manager import get_theme_manager
from app.ui.theme.tokens import Tokens
from tests.ui.test_movie_candidate_playback import candidates as candidates, found
from tests.ui.test_movie_progressive_search import cms as cms, wait
from tests.ui.test_movie_progressive_ui import screen as screen
from tests.ui.test_movie_voice_window import scene as scene

pytestmark = pytest.mark.ui


@pytest.mark.parametrize("theme,scale,width", [("dark", 1, 170), ("light", 1.5, 140)])
def test_poster_draws_source_inside_card_without_hiding_title_or_version(candidates, theme, scale, width):
    from PySide6.QtGui import QIcon
    app = candidates.app
    manager = get_theme_manager(app)
    original = manager.mode
    manager.set_theme(theme, persist=False)
    listing = QListWidget()
    delegate = _PosterWallDelegate(listing)
    title = "很长的电影片名" * 8
    source = "很长的来源名称" * 8
    data = dict(vod_name=title, vod_year="2026", vod_remarks="导演剪辑版", _source=source)
    item = QListWidgetItem()
    item.setData(Qt.UserRole, data)
    item.setIcon(QIcon(_make_poster_placeholder(title)))
    listing.addItem(item)
    image = QImage(QSize(round(width * scale), round(292 * scale)), QImage.Format_ARGB32)
    image.setDevicePixelRatio(scale)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    traces = []

    class Recorder:
        def __getattr__(self, attr):
            return getattr(painter, attr)

        def drawText(self, x, baseline, label):
            metrics = painter.fontMetrics()
            traces.append((label, baseline - metrics.ascent(), baseline + metrics.descent(),
                           x, metrics.horizontalAdvance(label), painter.pen().color()))
            painter.drawText(x, baseline, label)

    try:
        option = QStyleOptionViewItem()
        option.rect = QRect(0, 0, width, 292)
        delegate.paint(Recorder(), option, listing.indexFromItem(item))
        assert len(traces) == 4
        assert traces[0][0].startswith("很长的电影片名") and traces[0][0].endswith("…")
        assert traces[1][0] == "导演剪辑版"
        assert traces[2][0] == "2026"
        assert traces[3][0].startswith("很长的来") and traces[3][0].endswith("…")
        assert traces[3][5] == QColor(Tokens.TEXT_SECONDARY)
        assert all(214 < top < bottom < 292 and 0 <= x < x + extent <= width
                   for _, top, bottom, x, extent, _ in traces)
        assert traces[0][2] < traces[1][1] and traces[1][2] < traces[2][1]
        assert traces[3][3] + traces[3][4] < traces[2][3]
    finally:
        painter.end()
        listing.close()
        manager.set_theme(original, persist=False)


def test_same_title_tooltips_and_list_to_poster_toggle_keep_the_exact_candidate(candidates):
    panel = candidates.panel
    rows = found(candidates)
    for source, item in rows.items():
        assert item.toolTip().endswith("来源：" + source)
        assert not item.icon().isNull()  # These rows deliberately have no poster URL.
    for index in range(panel.list_results.count()):
        row = panel.list_results.item(index)
        source = row.data(Qt.UserRole)["_source"]
        assert row.text().endswith(source)
        assert row.toolTip() == rows[source].toolTip()
    panel._on_toggle_view()
    assert panel.stack_results.currentWidget() is panel.list_results
    panel._on_toggle_view()
    assert panel.stack_results.currentWidget() is panel.list_posters
    panel.list_posters.setCurrentItem(rows["非凡资源"])
    assert not panel.list_posters.viewport().grab().isNull()
    panel.list_posters.itemDoubleClicked.emit(rows["非凡资源"])
    wait(lambda: candidates.mpv.loadfile.called)
    candidates.mpv.loadfile.assert_called_once_with("https://media-1.invalid/movie.m3u8")
