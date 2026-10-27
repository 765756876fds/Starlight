"""
音源管理对话框（GlassDialog 无边框）
"""
import threading

from PySide6.QtCore import Qt, Signal
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
from ..widgets.glass_msg import (
    glass_critical,
    glass_info,
    glass_question,
    glass_warning,
)


def detect_plugin_type(content: str) -> str:
    """判断 JS 插件类型：'lx'（洛雪）或 'musicfree'。

    必须读全文判断：混淆插件的标记（require(/exports/module.exports）常出现
    在第 500 字节之后，只读头部会把它们误判成"非插件"，退回 JSON 导入后只
    存空壳元信息、不复制文件 → 插件永远加载不出来（开心汽水 曾因此丢失）。
    LX 特征：文件头 JSDoc 注释里的 @name（LX 规范要求）。MusicFree 特征：
    exports/require 结构。两类标记同时存在时按 MusicFree 处理（exports 结构
    更强、更接近可加载证据）。都没有明确标记时按 MusicFree 候选交给桥接
    校验（加载失败会显示错误，比静默建空壳强）。
    """
    c = content or ""
    has_lx = "@name" in c
    has_mf = ("module.exports" in c or "exports" in c or "require(" in c)
    if has_lx and not has_mf:
        return "lx"
    return "musicfree"


class SourceManageDialog(GlassDialog):
    """音源管理对话框"""

    # 后台线程 → 主线程回传插件刷新结果（worker 线程无 Qt 事件循环，必须走 Signal）
    _plugin_refresh_done = Signal(bool, int, str)  # (ok, 插件数, 文件名)

    def __init__(self, parent=None, on_sources_changed=None, manager=None,
                 reload_plugins=None):
        super().__init__("音乐源管理", parent)
        self.resize(600, 400)
        # 构造注入（默认从磁盘自建，向后兼容；测试/复用可传入同一实例）
        self.manager = manager if manager is not None else MusicSourceManager()
        # 音源列表变化（导入/删除/启停）后回调，用于把变更同步进在线播放链
        self.on_sources_changed = on_sources_changed
        # JS 插件桥接重载回调：Callable[[], tuple[bool, int]] → (ok, 插件数)。
        # 导入插件后自动调用，取消用户手动「刷新插件」步骤；为 None 时只复制文件。
        self.reload_plugins = reload_plugins
        # 刷新期间置 True：setCheckState 触发的 itemChanged 不视为用户操作
        self._loading = False
        self._plugin_refresh_done.connect(self._on_plugin_refresh_done)

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
            import os
            import shutil
            from app.core.path_resolve import music_sources_dir

            filename = os.path.basename(path)
            ext = os.path.splitext(filename)[1].lower()

            # JS 文件一律按插件处理：全文检测类型 → 复制到对应插件目录 →
            # 自动刷新桥接（后台线程），向用户报告真实结果。
            if ext == ".js":
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                plugin_type = detect_plugin_type(content)
                sub = "lx" if plugin_type == "lx" else "musicfree"
                # R4: 插件写用户可写目录（frozen 态 = USER_DATA_DIR/data/music_sources）
                target_dir = os.path.join(str(music_sources_dir()), sub)
                os.makedirs(target_dir, exist_ok=True)
                target_path = os.path.join(target_dir, filename)
                shutil.copy2(path, target_path)
                self._finish_plugin_import(plugin_type, filename, target_path)
                return

            # 普通 JSON 音源，走原有逻辑
            source = self.manager.import_from_json(path)
            glass_info(self, "成功", f"已导入音源: {source.name}")
            self._notify_changed()
            self._refresh_list()
        except Exception as e:
            glass_critical(self, "导入失败", f"错误: {e!s}")

    def _finish_plugin_import(self, plugin_type: str, filename: str,
                              target_path: str) -> None:
        """插件文件已复制 → 触发桥接刷新，完成后报告真实结果。

        报告只读事实：文件已复制；「可用」以刷新结果为准，刷新失败不假装成功。
        """
        self._refresh_list()
        if not self.reload_plugins:
            glass_info(
                self,
                "导入完成",
                f"已复制 {plugin_type} 插件: {filename}\n"
                f"位置: {target_path}\n"
                f"请点击设置页「刷新插件」按钮加载新插件。",
            )
            return
        self.btn_import_file.setEnabled(False)

        def worker():
            try:
                ok, n = self.reload_plugins()
            except Exception:
                ok, n = False, 0
            self._plugin_refresh_done.emit(ok, n, filename)

        threading.Thread(target=worker, daemon=True).start()

    def _on_plugin_refresh_done(self, ok: bool, n: int, filename: str) -> None:
        """插件刷新完成：恢复按钮，向用户报告真实结果，并同步外部。"""
        self.btn_import_file.setEnabled(True)
        if ok:
            glass_info(
                self,
                "导入成功",
                f"插件 {filename} 已导入并刷新完成\n"
                f"当前共加载 {n} 个插件，搜索界面已同步，可直接搜索。",
            )
        else:
            glass_critical(
                self,
                "导入完成但刷新失败",
                f"插件文件 {filename} 已复制，但插件刷新失败"
                f"（当前已加载 {n} 个插件）。\n"
                f"请稍后在设置页点击「刷新插件」重试，或查看插件列表中的错误信息。",
            )
        self._notify_changed()

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
            glass_critical(self, "导入失败", f"错误: {e!s}")

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
