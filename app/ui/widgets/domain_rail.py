from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QSizePolicy, QToolButton, QVBoxLayout

from ..theme.tokens import Tokens
from .icon_utils import lucide_icon


class DomainRail(QFrame):
    def __init__(self, shell):
        super().__init__(shell)
        self.shell = shell
        self.setObjectName("domainRail")
        self.setStyleSheet(
            f"QFrame#domainRail {{ background: {Tokens.BG_BASE}; border-right: 1px solid {Tokens.BORDER}; }}"
            f"QToolButton {{ text-align:left; border:0; border-radius:7px; padding:5px 8px; color:{Tokens.TEXT_SECONDARY}; }}"
            f"QToolButton:hover {{background:{Tokens.BG_HOVER};color:{Tokens.TEXT_PRIMARY};}}"
            f"QToolButton:checked {{background:{Tokens.BG_ACTIVE};color:{Tokens.TEXT_PRIMARY};font-weight:600;}}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 14, 8, 10)
        layout.setSpacing(3)
        self.brand = QLabel("星芒")
        font = self.brand.font()
        font.setPointSize(17)
        font.setBold(True)
        self.brand.setFont(font)
        self.brand.setStyleSheet("font-size:22px;font-weight:650;")
        self.brand.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.brand)
        layout.addSpacing(12)
        self.domains, self.library = {}, {}
        def add(label, icon, group, callback):
            btn = QToolButton()
            btn.setText(label)
            btn.setIcon(lucide_icon(icon, 18, Tokens.TEXT_PRIMARY))
            btn.setAccessibleName(label)
            btn.setToolTip(label)
            btn.setCheckable(True)
            btn.setMinimumHeight(33)
            btn.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            btn.clicked.connect(lambda _checked=False: callback(label))
            group[label] = btn
            layout.addWidget(btn)
        for label, icon in (("音乐","music"),("电影","play"),("游戏","grid"),("语音","mic")):
            add(label,icon,self.domains,shell.open_domain)
        layout.addSpacing(12)
        self.section = QLabel("音乐资料库")
        self.section.setProperty("class","secondary")
        layout.addWidget(self.section)
        for label, icon in (("发现音乐","search"),("榜单","list"),("本地","book-open"),("收藏","heart"),("歌单","grid"),("历史","rotate-ccw")):
            add(label,icon,self.library,shell.open_music)
        layout.addStretch()
        add("通知中心","info",self.domains,shell.open_domain)
        add("设置","settings-2",self.domains,shell.open_domain)
        self.compact = None
        self.set_compact(False)

    def set_compact(self, compact):
        if self.compact == compact:
            return
        self.compact = compact
        self.setFixedWidth(58 if compact else 142)
        self.brand.setText("芒" if compact else "星芒")
        self.section.setVisible(not compact)
        for btn in (*self.domains.values(),*self.library.values()):
            btn.setToolButtonStyle(Qt.ToolButtonIconOnly if compact else Qt.ToolButtonTextBesideIcon)
