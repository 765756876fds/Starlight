"""
音源管理对话框（GlassDialog 无边框）
"""
from PySide6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QPushButton, QLineEdit, QLabel, QFileDialog, QCheckBox
)
from PySide6.QtCore import Qt

from ..widgets.glass_msg import glass_info, glass_critical, glass_warning, glass_question

from app.music.source_manager import MusicSourceManager, MusicSource
from ..widgets.glass_dialog import GlassDialog


class SourceManageDialog(GlassDialog):
    """音源管理对话框"""

    def __init__(self, parent=None, on_sources_changed=None, manager=None):
        super().__init__("音乐源管理", parent)
        self.resize(600, 400)
        # 构造注入（默认从磁盘自建，向后兼容；测试/复用可传入同一实例）
        self.manager = manager if manager is not None else MusicSourceManager()
        # 音源列表变化（导入/删除/启停）后回调，用于把变更同步进在线播放链
        self.on_sources_changed = on_sources_changed
        # 刷新期间置 True：setCheckState 触发的 itemChanged 不视为用户操作
        self._loading = False

        root = self.body_layout

        # 说明
        root.addWidget(QLabel("支持添加自定义在线音乐源，格式参考洛雪音乐"))

        # 音源列表
        self.list_widget = QListWidget()
        root.addWidget(self.list_widget)

        # 按钮行
        btn_row = QHBoxLayout()

        self.btn_import_file = QPushButton("从本地文件导入")
        self.btn_import_file.clicked.connect(self._import_from_file)
        btn_row.addWidget(self.btn_import_file)

        self.btn_import_url = QPushButton("从URL导入")
        self.btn_import_url.clicked.connect(self._import_from_url)
        btn_row.addWidget(self.btn_import_url)

        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.btn_delete)

        btn_row.addStretch()
        root.addLayout(btn_row)

        # URL输入
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("音源URL:"))
        self.inp_url = QLineEdit()
        self.inp_url.setPlaceholderText("https://example.com/source.json")
        url_row.addWidget(self.inp_url)
        root.addLayout(url_row)

        # itemChanged 只连接一次（避免多次 _refresh_list 后重复触发回调）
        self.list_widget.itemChanged.connect(self._on_item_changed)
        self._refresh_list()

    def _refresh_list(self):
        """刷新音源列表"""
        self._loading = True
        try:
            self.list_widget.clear()
            sources = self.manager.get_all_sources()
            if not sources:
                item = QListWidgetItem("暂无自定义音源")
                item.setFlags(Qt.NoItemFlags)
                self.list_widget.addItem(item)
                return

            for s in sources:
                status = "开" if s.enabled else "关"
                item = QListWidgetItem(f"[{status}] {s.name} - {s.api_url}")
                item.setData(Qt.UserRole, s.name)
                # 双击切换启用/禁用
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
                self.list_widget.addItem(item)
        finally:
            self._loading = False

    def _on_item_changed(self, item: QListWidgetItem):
        """切换启用状态（仅响应刷新之外的用户操作）"""
        if self._loading:
            return
        name = item.data(Qt.UserRole)
        if name:
            self.manager.enable_source(name, item.checkState() == Qt.Checked)
            self._notify_changed()

    def _notify_changed(self):
        """通知外部同步音源变更（失败不影响 UI 操作）。"""
        if self.on_sources_changed:
            try:
                self.on_sources_changed()
            except Exception as e:
                print(f"[UI] 音源同步回调失败: {e}")

    def _import_from_file(self):
        """从本地文件导入"""
        path, _ = QFileDialog.getOpenFileName(
            self, "选择音源文件", "", "音源文件 (*.json *.js);;JSON Files (*.json);;JS Files (*.js);;All Files (*.*)"
        )
        if not path:
            return
        try:
            source = self.manager.import_from_json(path)
            glass_info(self, "成功", f"已导入音源: {source.name}")
            self._notify_changed()
            self._refresh_list()
        except Exception as e:
            glass_critical(self, "导入失败", f"错误: {str(e)}")

    def _import_from_url(self):
        """从URL导入"""
        url = self.inp_url.text().strip()
        if not url:
            glass_warning(self, "提示", "请输入音源URL")
            return
        try:
            source = self.manager.import_from_url(url)
            glass_info(self, "成功", f"已导入音源: {source.name}")
            self._notify_changed()
            self._refresh_list()
        except Exception as e:
            glass_critical(self, "导入失败", f"错误: {str(e)}")

    def _delete_selected(self):
        """删除选中的音源"""
        item = self.list_widget.currentItem()
        if not item:
            return
        name = item.data(Qt.UserRole)
        if not name:
            return
        if not glass_question(self, "确认删除", f"确定删除音源 {name} 吗？"):
            return
        self.manager.remove_source(name)
        self._notify_changed()
        self._refresh_list()
