"""GlassDialog：全局无边框深色半透明圆角对话框基类。

消灭原生白色标题栏/白边（FramelessWindowHint + WA_TranslucentBackground），
圆角深色容器由全局 QSS（#GlassDialogShell）绘制，内部结构：
  ┌ 标题条（纯文本标题 + Lucide x 关闭）┐
  ├────────────────────────────────────┤
  │  body（子类内容区）                │
  └────────────────────────────────────┘
子类用法：
    class MyDlg(GlassDialog):
        def __init__(self, parent=None):
            super().__init__("标题", parent)
            lay = QVBoxLayout(self.body)   # 内容加到 body
    （或直接 QVBoxLayout(self.body_layout)，二选一）
"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget,
)

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon


class GlassDialog(QDialog):
    def __init__(self, title: str = "", parent=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle(title or " ")
        self.setModal(True)

        # 外壳：圆角深色半透明容器（QSS #GlassDialogShell 负责圆角/边框/底色）
        self._shell = QWidget(self)
        self._shell.setObjectName("GlassDialogShell")
        root = QVBoxLayout(self._shell)
        root.setContentsMargins(Tokens.SPACING_MD, Tokens.SPACING_SM,
                                Tokens.SPACING_MD, Tokens.SPACING_MD)
        root.setSpacing(Tokens.SPACING_SM)

        # 标题条：纯文本标题 + Lucide x 关闭
        tb = QHBoxLayout()
        tb.setSpacing(Tokens.SPACING_SM)
        self.lbl_title = QLabel(title or " ")
        self.lbl_title.setStyleSheet(
            f"font-size: {Tokens.FONT_SIZE_TITLE}px; font-weight: bold; color: {Colors.TEXT_PRIMARY};")
        self.btn_close = QPushButton()
        self.btn_close.setIcon(lucide_icon("x", 14, Colors.TEXT_SECONDARY))
        self.btn_close.setToolTip("关闭")
        self.btn_close.setFixedSize(30, 26)
        self.btn_close.clicked.connect(self.reject)
        tb.addWidget(self.lbl_title)
        tb.addStretch()
        tb.addWidget(self.btn_close)
        root.addLayout(tb)

        # 内容容器：子类把布局加进 self.body / self.body_layout
        self.body = QWidget()
        self.body_layout = QVBoxLayout(self.body)
        self.body_layout.setContentsMargins(0, 0, 0, 0)
        self.body_layout.setSpacing(Tokens.SPACING_SM)
        root.addWidget(self.body, 1)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self._shell)
        self._root_layout = outer

    # 方便子类：把外部布局整体塞进 body
    def set_body_layout(self, layout) -> None:
        """把已构建的 QLayout 挂到 body（替换 body_layout 默认空布局）。"""
        while self.body_layout.count():
            item = self.body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
        self.body.setLayout(layout)
        self.body_layout = layout

    # 键盘可达性：Esc 关闭（原生 Dialog 无边框后仍保留）
    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
