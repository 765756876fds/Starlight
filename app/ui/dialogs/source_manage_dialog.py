"""
音源管理对话框（GlassDialog 无边框）
"""
from pathlib import Path
from urllib.parse import urlsplit

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
)

from app.music.source_manager import MusicSourceManager

from ..widgets.glass_dialog import GlassDialog
from ..widgets.latest_task import LatestTask
from ..widgets.glass_msg import (
    glass_critical,
    glass_info,
    glass_question,
    glass_warning,
)


class SourceManageDialog(GlassDialog):
    """管理普通入口使用的个人 HTTP JSON 音源。"""

    def __init__(self, parent=None, on_sources_changed=None, manager=None, active_sources=None):
        super().__init__("个人音源", parent)
        self.resize(620, 410)
        self.manager = manager if manager is not None else MusicSourceManager()
        self.on_sources_changed = on_sources_changed
        self._active_sources = active_sources
        self._config_read = LatestTask(self, timeout_ms=10000)
        self._config_read.completed.connect(self._finish_url_import)
        self.finished.connect(self._config_read.cancel)
        # 刷新期间置 True：setCheckState 触发的 itemChanged 不视为用户操作
        self._loading = False

        root = self.body_layout

        # 说明
        hint = QLabel("导入本人有权使用的 HTTP JSON 服务。勾选后参与搜索，可用曲目和音质以服务实际返回为准。"
                      "\nMusicFree 或洛雪 JS 插件须经审查，从受控入口启动。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        # 音源列表
        self.list_widget = QListWidget()
        root.addWidget(self.list_widget)

        # 按钮行
        btn_row = QHBoxLayout()

        self.btn_import_file = QPushButton("导入 JSON 文件")
        self.btn_import_file.clicked.connect(self._import_from_file)
        btn_row.addWidget(self.btn_import_file)

        self.btn_import_url = QPushButton("导入 JSON 链接")
        self.btn_import_url.clicked.connect(self._import_from_url)
        btn_row.addWidget(self.btn_import_url)

        self.btn_delete = QPushButton("删除选中")
        self.btn_delete.clicked.connect(self._delete_selected)
        btn_row.addWidget(self.btn_delete)

        btn_row.addStretch()
        root.addLayout(btn_row)

        # URL输入
        url_row = QHBoxLayout()
        url_row.addWidget(QLabel("配置链接"))
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

            registered = set(self._active_sources()) if self._active_sources else None
            for s in sources:
                item = QListWidgetItem()
                item.setText(self._label(s, registered))
                item.setData(Qt.UserRole, s.name)
                # 双击切换启用/禁用
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable | Qt.ItemIsEnabled)
                item.setCheckState(Qt.Checked if s.enabled else Qt.Unchecked)
                self.list_widget.addItem(item)
        finally:
            self._loading = False

    @staticmethod
    def _label(source, registered):
        state = "已停用"
        if source.enabled:
            state = "未接入搜索" if registered is not None and source.name not in registered else "已启用"
        try:
            host = urlsplit(source.api_url).hostname or "地址未配置"
        except (TypeError, ValueError, AttributeError):
            host = "地址无效"
        return f"{source.name} · {state} · {host}"

    def _on_item_changed(self, item: QListWidgetItem):
        """切换启用状态（仅响应刷新之外的用户操作）"""
        if self._loading:
            return
        name = item.data(Qt.UserRole)
        if name:
            self.manager.enable_source(name, item.checkState() == Qt.Checked)
            self._notify_changed()
            source = next((entry for entry in self.manager.get_all_sources() if entry.name == name), None)
            if source is not None:
                registered = set(self._active_sources()) if self._active_sources else None
                self._loading = True
                try:
                    with QSignalBlocker(self.list_widget):
                        item.setText(self._label(source, registered))
                finally:
                    self._loading = False

    def _notify_changed(self):
        """通知外部同步音源变更（失败不影响 UI 操作）。"""
        if self.on_sources_changed:
            try:
                self.on_sources_changed()
            except Exception as e:
                print(f"[UI] 音源同步回调失败: {e}")

    def _import_from_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "选择音源配置", "", "JSON 配置 (*.json);;所有文件 (*.*)"
        )
        if not path:
            return
        if Path(path).suffix.lower() != ".json":
            glass_warning(self, "无法导入", "这里仅接受 JSON 配置。JS 插件须经审查，从受控入口启动。")
            return
        try:
            source = self.manager.import_from_json(path)
        except (OSError, ValueError) as error:
            self._report_import_error(error)
            return
        self._show_import_result(source)

    def _import_from_url(self):
        url = self.inp_url.text().strip()
        if not url:
            glass_warning(self, "缺少链接", "请输入 HTTP(S) 音源配置链接。")
            return
        self.btn_import_url.setEnabled(False)
        self.btn_import_url.setText("正在读取…")
        self._config_read.submit("config", lambda: self.manager.fetch_configuration(url))

    def _finish_url_import(self, _key, payload, error):
        self.btn_import_url.setText("导入 JSON 链接")
        self.btn_import_url.setEnabled(True)
        if error:
            if isinstance(error, ValueError):
                self._report_import_error(error)
            else:
                glass_critical(self, "导入失败", "暂时无法读取配置链接，请检查地址和网络后重试。")
            return
        try:
            source = self.manager.import_configuration(payload)
        except (OSError, ValueError) as error:
            self._report_import_error(error)
            return
        self._show_import_result(source)

    def _report_import_error(self, error):
        message = ("已存在同名音源，请为新配置使用其他名称。"
                   if str(error) == "已存在同名音源，请使用其他名称"
                   else "请选择有效的 HTTP JSON 配置，确认名称、服务地址及文件权限后重试。")
        glass_critical(self, "导入失败", message)

    def _show_import_result(self, source):
        self._notify_changed()
        self._refresh_list()
        if not source.enabled:
            state = "配置为停用，勾选后可参与搜索。"
        elif self._active_sources and source.name not in self._active_sources():
            state = "配置已保存，但未接入搜索。请检查服务地址或音源名称是否冲突。"
        else:
            state = "已接入搜索，可通过搜索和播放确认服务是否可用。"
        glass_info(self, "配置已保存", f"{source.name}\n{state}")

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
