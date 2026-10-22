"""Glass 自定义顶栏（无边框主窗口用）：标题 + 最小化 + 关闭 + 拖动。"""
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon


class GlassTitleBar(QWidget):
    """无边框主窗口的顶栏。

    - 左：应用标题（半透明次要色）
    - 右：最小化（—）/ 关闭（✕）
    - 按住顶栏空白处拖动窗口（mousePress 记录偏移 + mouseMove 移动）

    close_clicked：点「✕」时发射（不再直接 close()，由主窗口决定隐藏/退出弹窗，
    避免 QCloseEvent.spontaneous()==False 被误判为编程式关闭）。
    """

    close_clicked = Signal()

    _HEIGHT = 36

    def __init__(self, title: str = "LoL Music Agent", parent: QWidget = None):
        super().__init__(parent)
        self.setObjectName("GlassTitleBar")
        self.setFixedHeight(self._HEIGHT)
        self._drag_offset: QPoint | None = None

        layout = QHBoxLayout(self)
        layout.setContentsMargins(Tokens.SPACING_MD, 0, Tokens.SPACING_SM, 0)
        layout.setSpacing(Tokens.SPACING_XS)

        self.lbl_title = QLabel(title)
        layout.addWidget(self.lbl_title)
        layout.addStretch(1)

        self.btn_min = QPushButton()
        self.btn_min.setObjectName("btn_min")
        self.btn_min.setIcon(lucide_icon("minus", 14, Colors.TEXT_SECONDARY))
        self.btn_min.setToolTip("最小化")
        self.btn_min.setFixedSize(40, 26)
        layout.addWidget(self.btn_min)

        self.btn_close = QPushButton()
        self.btn_close.setObjectName("btn_close")
        self.btn_close.setIcon(lucide_icon("x", 13, Colors.TEXT_SECONDARY))
        self.btn_close.setToolTip("关闭")
        self.btn_close.setFixedSize(40, 26)
        layout.addWidget(self.btn_close)

        self.btn_min.clicked.connect(self._on_minimize)
        self.btn_close.clicked.connect(self._on_close)

    # ---- 窗口控制 ----
    def _window(self) -> QWidget:
        return self.window()

    def _on_minimize(self) -> None:
        w = self._window()
        if w is not None:
            w.showMinimized()

    def _on_close(self) -> None:
        # 只发信号，不直接 close()：主窗口据此区分「用户点 X」与「编程式关闭」
        self.close_clicked.emit()

    # ---- 拖动 ----
    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.LeftButton:
            self._drag_offset = (
                event.globalPosition().toPoint() - self._window().frameGeometry().topLeft()
            )
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:
        if self._drag_offset is not None and event.buttons() & Qt.LeftButton:
            w = self._window()
            if w is not None:
                w.move(event.globalPosition().toPoint() - self._drag_offset)
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        self._drag_offset = None
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:
        # 双击顶栏最大化/还原（保持系统习惯）
        if event.button() == Qt.LeftButton:
            w = self._window()
            if w is not None and hasattr(w, "isMaximized"):
                if w.isMaximized():
                    w.showNormal()
                else:
                    w.showMaximized()
        super().mouseDoubleClickEvent(event)
