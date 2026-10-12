"""设置 tab：音乐目录管理 / 语音 TTS / LoL 开关 / 界面刷新间隔。

- 音乐目录：config/music_dirs.json（路径/启用/格式过滤/排除子目录），[扫描全部] 走
  music.scanner 直接赋值 + QThread 调 music.rescan()（不碰 music 业务逻辑）
- TTS：更新 settings.yaml tts 节 + 调 TTSController set_voice/set_rate/set_volume 立即生效
- LoL：连招总开关（不持久化）、仅 LoL 前台 + 装备提醒（持久化 config/ui_prefs.json）
- 界面：刷新间隔（持久化 ui_prefs.json，经 on_refresh_changed 回调应用到各面板 timer）
"""
from __future__ import annotations

from typing import Callable, Optional
from app.ui.theme.colors import Colors


from PySide6.QtCore import Qt, QThread, QObject, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QVBoxLayout, QWidget,
)

from ..settings_store import (
    ALL_EXTENSIONS, load_music_dirs, save_music_dirs,
    load_ui_prefs, save_ui_prefs, save_settings_yaml, reset_ui_prefs, reset_music_dirs,
    SETTINGS_PATH as _SETTINGS_PATH,
)
from ..widgets.toggle_button import ToggleButton

# 内置常用 TTS 音色（可编辑下拉，支持手动输入任意 edge-tts 语音名）
_TTS_VOICES = [
    "zh-CN-XiaoxiaoNeural",   # 晓晓（女）
    "zh-CN-XiaoyiNeural",     # 晓伊（女）
    "zh-CN-YunxiNeural",      # 云希（男）
    "zh-CN-YunjianNeural",    # 云健（男）
    "zh-CN-YunyangNeural",    # 云扬（男）
    "zh-CN-XiaochenNeural",   # 晓辰（女）
    "zh-CN-XiaohanNeural",    # 晓涵（女）
    "zh-CN-XiaomengNeural",   # 晓梦（女）
    "zh-TW-HsiaoChenNeural",  # 曉臻（女，台湾）
    "en-US-AriaNeural",       # Aria（英女）
    "en-US-GuyNeural",        # Guy（英男）
]


class _ScanWorker(QObject):
    """后台扫描 worker（复用 QThread 模式，避免大目录扫描卡 UI）。"""
    done = Signal(dict)
    failed = Signal(str)

    def __init__(self, music):
        super().__init__()
        self._music = music

    def run(self):
        try:
            result = self._music.rescan()
            self.done.emit(result or {})
        except Exception as e:
            self.failed.emit(str(e))


class _DirEditDialog(QDialog):
    """单个目录编辑：格式过滤多选 + 排除子目录。"""

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.setWindowTitle("编辑音乐目录")
        self.setModal(True)
        self._exts = set(entry.get("extensions") or ALL_EXTENSIONS)
        root = QVBoxLayout(self)
        root.addWidget(QLabel(f"<b>{entry.get('path', '')}</b>"))
        root.addWidget(QLabel("格式过滤（勾选要扫描的格式）："))
        self._boxes = []
        frow = QHBoxLayout()
        for ext in ALL_EXTENSIONS:
            cb = QCheckBox(ext)
            cb.setChecked(ext in self._exts)
            self._boxes.append(cb)
            frow.addWidget(cb)
        root.addLayout(frow)
        self.cb_exclude = QCheckBox("排除子目录（只扫该目录本身，不递归）")
        self.cb_exclude.setChecked(bool(entry.get("exclude_subdirs", False)))
        root.addWidget(self.cb_exclude)
        btns = QHBoxLayout()
        btn_ok = QPushButton("确定")
        btn_ok.setProperty("class", "primary")
        btn_cancel = QPushButton("取消")
        btn_ok.clicked.connect(self.accept)
        btn_cancel.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(btn_cancel)
        btns.addWidget(btn_ok)
        root.addLayout(btns)

    def result_entry(self) -> dict:
        exts = [b.text() for b in self._boxes if b.isChecked()]
        return {"extensions": exts, "exclude_subdirs": self.cb_exclude.isChecked()}


class SettingsPanel(QWidget):
    def __init__(self, music=None, tts=None, combo_manager=None,
                 equip_toggle: Optional[Callable[[bool], None]] = None,
                 on_refresh_changed: Optional[Callable[[int], None]] = None,
                 overlay_toggle: Optional[Callable[[bool], None]] = None,
                 overlay_enabled: bool = True,
                 parent=None):
        super().__init__(parent)
        self._music = music
        self._tts = tts
        self._combo = combo_manager
        self._equip_toggle = equip_toggle
        self._on_refresh_changed = on_refresh_changed
        self._overlay_toggle = overlay_toggle
        self._overlay_enabled = bool(overlay_enabled)
        self._scan_thread = None

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        root = QVBoxLayout(inner)
        root.setSpacing(10)
        scroll.setWidget(inner)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(scroll)

        self._build_music_group(root)
        self._build_tts_group(root)
        self._build_lol_group(root)
        self._build_ui_group(root)
        root.addStretch(1)
        self._build_reset_row(root)

    # ---------- 🎵 音乐目录管理 ----------

    def _build_music_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("🎵 音乐目录管理")
        lay = QVBoxLayout(box)

        # 目录列表容器：QScrollArea + 行 widget（避开 QListWidget setItemWidget 压缩）
        list_scroll = QScrollArea()
        list_scroll.setWidgetResizable(True)
        list_scroll.setMinimumHeight(100)  # 有内容时按内容撑高，无内容不占大块
        self._dirs_container = QWidget()
        self._dirs_layout = QVBoxLayout(self._dirs_container)
        self._dirs_layout.setContentsMargins(4, 4, 4, 4)
        self._dirs_layout.setSpacing(4)
        list_scroll.setWidget(self._dirs_container)
        lay.addWidget(list_scroll)

        btns = QHBoxLayout()
        self.btn_add_dir = QPushButton("+ 添加目录")
        self.btn_add_dir.clicked.connect(self._on_add_dir)
        self.btn_scan_all = QPushButton("扫描全部")
        self.btn_scan_all.setProperty("class", "primary")
        self.btn_scan_all.clicked.connect(self._on_scan_all)
        self.btn_source_manage = QPushButton("音源管理…")
        self.btn_source_manage.clicked.connect(self._on_source_manage)
        self.lbl_scan_status = QLabel("")
        self.lbl_scan_status.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        btns.addWidget(self.btn_add_dir)
        btns.addWidget(self.btn_scan_all)
        btns.addWidget(self.btn_source_manage)
        btns.addWidget(self.lbl_scan_status)
        btns.addStretch()
        lay.addLayout(btns)
        root.addWidget(box)
        self._dirs: list = load_music_dirs()
        self._render_dirs()

    def _on_source_manage(self):
        """打开音源管理对话框（从音乐面板挪到设置页）。"""
        if self._music is None:
            return
        from app.ui.dialogs.source_manage_dialog import SourceManageDialog
        dlg = SourceManageDialog(self, on_sources_changed=self._music.sync_custom_sources)
        dlg.exec()

    def _render_dirs(self) -> None:
        while self._dirs_layout.count():
            it = self._dirs_layout.takeAt(0)
            w = it.widget()
            if w is not None:
                w.deleteLater()
        if not self._dirs:
            lbl = QLabel("暂无音乐目录，点击「+ 添加目录」添加")
            lbl.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
            self._dirs_layout.addWidget(lbl)
            return
        for i, d in enumerate(self._dirs):
            self._dirs_layout.addWidget(self._make_dir_row(i, d))

    def _make_dir_row(self, idx: int, entry: dict) -> QWidget:
        row = QWidget()
        row.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        h = QHBoxLayout(row)
        h.setContentsMargins(0, 2, 0, 2)
        cb = QCheckBox("启用")
        cb.setChecked(bool(entry.get("enabled", True)))
        cb.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        cb.toggled.connect(lambda on, i=idx: self._on_toggle_dir(i, on))
        h.addWidget(cb)
        lbl = QLabel(entry.get("path", "?"))
        lbl.setWordWrap(True)
        lbl.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        h.addWidget(lbl, 1)
        btn_edit = QPushButton("编辑")
        btn_edit.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        btn_edit.setMinimumWidth(56)
        btn_edit.setFixedHeight(28)
        btn_edit.clicked.connect(lambda _=False, i=idx: self._on_edit_dir(i))
        h.addWidget(btn_edit)
        btn_del = QPushButton("×")
        btn_del.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        btn_del.setMinimumWidth(32)
        btn_del.setFixedHeight(28)
        btn_del.setStyleSheet(f"color: {Colors.DANGER}; font-weight: bold;")
        btn_del.setToolTip("删除此目录")
        btn_del.clicked.connect(lambda _=False, i=idx: self._on_del_dir(i))
        h.addWidget(btn_del)
        return row

    def _save_dirs(self) -> None:
        if save_music_dirs(self._dirs):
            self._render_dirs()

    def _on_toggle_dir(self, idx: int, on: bool) -> None:
        self._dirs[idx]["enabled"] = bool(on)
        self._save_dirs()

    def _on_add_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "选择音乐目录")
        if not path:
            return
        self._dirs.append({
            "path": path,
            "enabled": True,
            "extensions": list(ALL_EXTENSIONS),
            "exclude_subdirs": False,
        })
        self._save_dirs()

    def _on_edit_dir(self, idx: int) -> None:
        dlg = _DirEditDialog(self._dirs[idx], self)
        if dlg.exec() != QDialog.Accepted:
            return
        entry = dlg.result_entry()
        self._dirs[idx].update(entry)
        self._save_dirs()

    def _on_del_dir(self, idx: int) -> None:
        self._dirs.pop(idx)
        self._save_dirs()

    def _scan_dirs(self) -> Optional[list]:
        """返回启用的目录配置；无启用目录时提示并返回 None。"""
        enabled = [d for d in self._dirs if d.get("enabled")]
        if not enabled:
            self.lbl_scan_status.setText("无启用的音乐目录")
            return None
        return enabled

    def _apply_scan_dirs(self, enabled: list) -> bool:
        """把启用目录应用到 music.scanner（直接赋值，零业务改动）。"""
        if self._music is None:
            return False
        exts = set()
        for d in enabled:
            for e in (d.get("extensions") or []):
                e = str(e).lower().lstrip(".")
                if e:
                    exts.add("." + e)
        if not exts:
            exts = {"." + e for e in ALL_EXTENSIONS}
        try:
            self._music.scanner.scan_dirs = [d["path"] for d in enabled]
            self._music.scanner.extensions = exts
            return True
        except Exception as e:
            print(f"[Settings] 应用扫描目录失败: {e}")
            return False

    def _on_scan_all(self) -> None:
        enabled = self._scan_dirs()
        if enabled is None or not self._apply_scan_dirs(enabled):
            return
        self.lbl_scan_status.setText("扫描中…")
        self.btn_scan_all.setEnabled(False)
        self._scan_thread = QThread()
        self._scan_worker = _ScanWorker(self._music)
        self._scan_worker.moveToThread(self._scan_thread)
        self._scan_thread.started.connect(self._scan_worker.run)
        self._scan_worker.done.connect(self._on_scan_done)
        self._scan_worker.failed.connect(self._on_scan_failed)
        self._scan_worker.done.connect(self._scan_thread.quit)
        self._scan_worker.failed.connect(self._scan_thread.quit)
        self._scan_thread.start()

    def _on_scan_done(self, result: dict) -> None:
        self.btn_scan_all.setEnabled(True)
        self.lbl_scan_status.setText(
            f"扫描完成：新增{result.get('added', 0)} 更新{result.get('updated', 0)} "
            f"跳过{result.get('skipped', 0)} 错误{result.get('errors', 0)}")

    def _on_scan_failed(self, err: str) -> None:
        self.btn_scan_all.setEnabled(True)
        self.lbl_scan_status.setText(f"扫描失败: {err}")

    # ---------- 🎤 语音 TTS ----------

    def _build_tts_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("🎤 语音 TTS")
        lay = QVBoxLayout(box)

        vrow = QHBoxLayout()
        vrow.addWidget(QLabel("音色："))
        self.cmb_voice = QComboBox()
        self.cmb_voice.setEditable(True)
        self.cmb_voice.addItems(_TTS_VOICES)
        self.cmb_voice.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        vrow.addWidget(self.cmb_voice, 1)
        lay.addLayout(vrow)

        self._rate_slider = QSlider(Qt.Horizontal)
        self._rate_slider.setRange(-50, 50)
        self._rate_slider.setValue(0)
        self.lbl_rate = QLabel("语速：+0%")
        self.lbl_rate.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(self.lbl_rate)
        lay.addWidget(self._rate_slider)

        self._vol_slider = QSlider(Qt.Horizontal)
        self._vol_slider.setRange(0, 100)
        self._vol_slider.setValue(0)
        self.lbl_vol = QLabel("音量：+0%")
        self.lbl_vol.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: 11px;")
        lay.addWidget(self.lbl_vol)
        lay.addWidget(self._vol_slider)

        root.addWidget(box)

        # 从 settings.yaml 加载当前值并接线
        self._load_tts_values()
        self.cmb_voice.currentTextChanged.connect(self._on_tts_change)
        self._rate_slider.valueChanged.connect(self._on_tts_change)
        self._vol_slider.valueChanged.connect(self._on_tts_change)

    def _load_tts_values(self) -> None:
        try:
            import yaml
            with open(_SETTINGS_PATH, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            tts_cfg = cfg.get("tts", {})
            voice = tts_cfg.get("edge_voice", "zh-CN-XiaoxiaoNeural")
            rate = tts_cfg.get("rate", "+0%")
            volume = tts_cfg.get("volume", "+0%")
        except Exception:
            return
        # 音色：已有值不在内置列表时补进去（可编辑下拉）
        if self.cmb_voice.findText(voice) < 0:
            self.cmb_voice.insertItem(0, voice)
        self.cmb_voice.setCurrentText(voice)
        try:
            self._rate_slider.setValue(int(str(rate).replace("%", "").replace("+", "")))
        except Exception:
            pass
        try:
            self._vol_slider.setValue(int(str(volume).replace("%", "").replace("+", "")))
        except Exception:
            pass
        self.lbl_rate.setText(f"语速：{self._rate_slider.value():+d}%")
        self.lbl_vol.setText(f"音量：{self._vol_slider.value():+d}%")

    def _on_tts_change(self, *_args) -> None:
        voice = self.cmb_voice.currentText().strip()
        rate = f"{self._rate_slider.value():+d}%"
        volume = f"{self._vol_slider.value():+d}%"
        self.lbl_rate.setText(f"语速：{rate}")
        self.lbl_vol.setText(f"音量：{volume}")
        # 持久化到 settings.yaml tts 节（带锁）
        save_settings_yaml({"tts": {
            "edge_voice": voice, "rate": rate, "volume": volume,
        }})
        # 立即生效（TTSController setter，下次生成即用新值）
        if self._tts is not None:
            try:
                self._tts.set_voice(voice)
                self._tts.set_rate(rate)
                self._tts.set_volume(volume)
            except Exception as e:
                print(f"[Settings] TTS 应用失败: {e}")

    # ---------- 🎮 LoL 开关 ----------

    def _build_lol_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("🎮 LoL")
        lay = QVBoxLayout(box)

        prefs = load_ui_prefs()
        self.btn_gate = ToggleButton("仅 LoL 前台时触发",
                                     bool(prefs.get("combo_window_gate", True)))
        self.btn_gate.setMinimumWidth(180)
        self.btn_gate.toggled_state.connect(self._on_gate)
        lay.addWidget(self.btn_gate)

        self.btn_equip = ToggleButton("装备提醒",
                                      bool(prefs.get("equip_notify", True)))
        self.btn_equip.setMinimumWidth(180)
        self.btn_equip.toggled_state.connect(self._on_equip)
        lay.addWidget(self.btn_equip)

        root.addWidget(box)

    def _on_gate(self, on: bool) -> None:
        if self._combo is not None:
            self._combo.set_window_gate(on)
        save_ui_prefs({"combo_window_gate": on})

    def _on_equip(self, on: bool) -> None:
        save_ui_prefs({"equip_notify": on})
        if self._equip_toggle is not None:
            self._equip_toggle(on)
        print(f"[Equipment] 装备提醒: {'开' if on else '关'}")  # 对齐 [Combo] 格式

    # ---------- 🖥️ 界面 ----------

    def _build_ui_group(self, root: QVBoxLayout) -> None:
        box = QGroupBox("🖥️ 界面")
        lay = QVBoxLayout(box)

        self.btn_overlay = ToggleButton("显示游戏内 Overlay", self._overlay_enabled)
        self.btn_overlay.setMinimumWidth(180)
        self.btn_overlay.toggled_state.connect(self._on_overlay_toggle)
        lay.addWidget(self.btn_overlay)

        irow = QHBoxLayout()
        irow.addWidget(QLabel("刷新间隔（毫秒）："))
        self.spin_refresh = QSpinBox()
        self.spin_refresh.setRange(100, 5000)
        self.spin_refresh.setSingleStep(100)
        self.spin_refresh.setValue(int(load_ui_prefs().get("ui_refresh_interval", 500)))
        irow.addWidget(self.spin_refresh)
        irow.addStretch()
        lay.addLayout(irow)
        lay.addWidget(QLabel("影响主界面各面板的定时刷新频率（当前值仅存 ui_prefs.json）。"))

        root.addWidget(box)

        self.spin_refresh.valueChanged.connect(self._on_refresh_changed)

    def _on_overlay_toggle(self, on: bool) -> None:
        if self._overlay_toggle is not None:
            self._overlay_toggle(on)

    def _build_reset_row(self, root: QVBoxLayout) -> None:
        """设置页底部：恢复默认设置（只重置设置页管理的配置）。"""
        row = QHBoxLayout()
        row.addStretch()
        btn = QPushButton("恢复默认设置")
        btn.setStyleSheet(f"color: {Colors.DANGER};")
        btn.setToolTip("重置界面偏好 / 悬浮窗设置 / 音乐目录 / TTS 音色")
        btn.clicked.connect(self._on_reset_defaults)
        row.addWidget(btn)
        root.addLayout(row)

    def _on_reset_defaults(self) -> None:
        ret = QMessageBox.question(
            self, "恢复默认设置",
            "将重置：界面偏好 / 悬浮窗设置 / 音乐目录 / TTS 音色\n\n"
            "不影响：连招配置 / mpv 路径 / 音源 / 自动化配置\n\n确定继续？",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret != QMessageBox.Yes:
            return
        reset_ui_prefs()
        reset_music_dirs()
        save_settings_yaml({"tts": {
            "edge_voice": "zh-CN-XiaoxiaoNeural",
            "rate": "+0%",
            "volume": "+0%",
        }})
        # 刷新面板 UI
        self._dirs = load_music_dirs()
        self._render_dirs()
        self._load_tts_values()
        prefs = load_ui_prefs()
        self.btn_gate.set_state(bool(prefs.get("combo_window_gate", True)))
        self.btn_equip.set_state(bool(prefs.get("equip_notify", True)))
        if self._overlay_toggle is not None:
            self._overlay_toggle(True)  # 先同步后端（set_enabled(True)）
        self.btn_overlay.set_state(True)  # 再同步 UI（blockSignals，不重复触发回调）
        self.spin_refresh.setValue(int(prefs.get("ui_refresh_interval", 500)))
        if self._on_refresh_changed is not None:
            self._on_refresh_changed(int(prefs.get("ui_refresh_interval", 500)))
        if self._tts is not None:
            try:
                self._tts.set_voice("zh-CN-XiaoxiaoNeural")
                self._tts.set_rate("+0%")
                self._tts.set_volume("+0%")
            except Exception as e:
                print(f"[Settings] TTS 重置应用失败: {e}")

    def _on_refresh_changed(self, ms: int) -> None:
        save_ui_prefs({"ui_refresh_interval": int(ms)})
        if self._on_refresh_changed is not None:
            self._on_refresh_changed(int(ms))
