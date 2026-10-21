"""链接解析输入框（GlassDialog）：输入视频网页链接 + 解析按钮。

第一阶段只做 UI 收集 + 面板后台解析 + 弹窗展示结果（不接播放器）。
"""
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QPushButton

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.glass_dialog import GlassDialog


class ParseLinkDialog(GlassDialog):
    """输入视频网页链接（爱奇艺/腾讯等），点「解析」返回链接给面板。"""

    def __init__(self, parent=None):
        super().__init__("链接解析", parent)
        lay = self.body_layout

        tip = QLabel("请输入视频网页链接（如爱奇艺/腾讯网页）")
        tip.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_BODY}px;")
        lay.addWidget(tip)

        self.edit_url = QLineEdit()
        self.edit_url.setPlaceholderText("https://www.iqiyi.com/v_xxx.html …")
        lay.addWidget(self.edit_url)

        # 第三方解析接口（可选）：填了则直接用「接口地址 + 视频链接」交给播放器，跳过本地 videodl
        tip2 = QLabel("自定义解析接口（可选，遇到 VIP 解析失败时可填）")
        tip2.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        lay.addWidget(tip2)
        self.edit_api = QLineEdit()
        self.edit_api.setPlaceholderText("https://jx.xxx.com/?url=")
        lay.addWidget(self.edit_api)

        row = QHBoxLayout()
        row.addStretch()
        self.btn_parse = QPushButton("解析")
        self.btn_parse.setProperty("class", "primary")
        self.btn_parse.setMinimumWidth(88)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setMinimumWidth(88)
        row.addWidget(self.btn_parse)
        row.addWidget(self.btn_cancel)
        lay.addLayout(row)

        self.btn_parse.clicked.connect(self.accept)
        self.btn_cancel.clicked.connect(self.reject)
        self.edit_url.returnPressed.connect(self.accept)
        self.edit_url.setFocus()

    def url(self) -> str:
        return self.edit_url.text().strip()

    def third_party_api(self) -> str:
        return self.edit_api.text().strip()
