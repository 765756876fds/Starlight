"""Exercise the real hero delegate's focused selection branch without game actions."""
import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QImage, QPainter, QStandardItem, QStandardItemModel
from PySide6.QtWidgets import QApplication, QStyle, QStyleOptionViewItem

from app.ui.panels.combo_panel import _HeroListDelegate
from app.ui.theme.colors import Colors
from app.ui.theme.manager import get_theme_manager


@pytest.fixture
def theme(tmp_path, monkeypatch):
    monkeypatch.setattr("app.ui.settings_store.UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    application = QApplication.instance() or QApplication([])
    manager = get_theme_manager(application)
    yield manager
    manager.set_theme("dark", persist=False)


@pytest.mark.parametrize("mode", ["dark", "light"])
@pytest.mark.parametrize("selected,focused", [(True, True), (True, False), (False, True), (False, False)])
def test_real_delegate_focus_border_uses_current_accent_and_restores_painter(
    theme, mode, selected, focused,
):
    theme.set_theme(mode, persist=False)
    image = QImage(160, 48, QImage.Format_ARGB32)
    image.fill(QColor(Colors.BG_BASE))
    model = QStandardItemModel()
    model.appendRow(QStandardItem("Hero"))
    option = QStyleOptionViewItem()
    option.rect = QRect(8, 8, 128, 28)
    option.state = QStyle.State_Enabled | QStyle.State_Active
    if selected:
        option.state |= QStyle.State_Selected
    if focused:
        option.state |= QStyle.State_HasFocus
    delegate = _HeroListDelegate()
    painter = QPainter(image)
    original_pen, original_transform = painter.pen(), painter.transform()
    try:
        delegate.paint(painter, option, model.index(0, 0))
        assert painter.pen() == original_pen
        assert painter.transform() == original_transform
    finally:
        painter.end()
    # Aliased drawRect reaches x + width, matching the item's inclusive right edge here.
    border = image.pixelColor(option.rect.right(), option.rect.top() + 8)
    assert (border == QColor(Colors.ACCENT)) is (selected and focused)
