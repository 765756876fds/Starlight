"""自动化面板：4 个开关 + 状态显示。"""
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QGroupBox, QLabel, QSizePolicy, QVBoxLayout,
    QWidget,
)

from ..widgets.toggle_button import ToggleButton
from ..widgets.icon_utils import lucide_icon
from ..dialogs.auto_queue_config_dialog import AutoQueueConfigDialog
from ..dialogs.ban_pick_config_dialog import BanPickConfigDialog
from ..theme.colors import Colors
from ..theme.tokens import Tokens


class AutomationPanel(QWidget):
    def __init__(self, service=None, lcu=None, combo_manager=None, parent=None):
        super().__init__(parent)
        self._svc = service
        self._lcu = lcu
        self._combo_manager = combo_manager
        layout = QVBoxLayout(self)

        box = QGroupBox("LoL 自动化")
        form = QVBoxLayout(box)

        self.lbl_state = QLabel("状态：?")
        self.lbl_phase = QLabel("游戏阶段：?")
        self._lbl_hint = QLabel("")
        self._lbl_hint.setStyleSheet(f"color: {Colors.TEXT_SECONDARY}; font-size: {Tokens.FONT_SIZE_SUB}px;")
        self._lbl_hint.setWordWrap(True)
        form.addWidget(self.lbl_phase)
        form.addWidget(self.lbl_state)
        form.addWidget(self._lbl_hint)

        self.btn_queue = ToggleButton("自动排队", False)
        self.btn_accept = ToggleButton("自动接受", False)
        self.btn_ban = ToggleButton("自动禁用", False)
        self.btn_pick = ToggleButton("自动选择", False)
        # 等宽自适应：Expanding 随行宽拉伸（最小 200 / 最大 300，同宽由相同约束保证）
        for _b in (self.btn_queue, self.btn_accept, self.btn_ban, self.btn_pick):
            _b.setMinimumWidth(200)
            _b.setMaximumWidth(300)
            _b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        def _row(btn, gear=None):
            r = QHBoxLayout()
            r.addWidget(btn)  # Expanding，自动拉伸
            if gear is not None:
                g = QPushButton()
                g.setIcon(lucide_icon("settings-2", 16, Colors.TEXT_SECONDARY))
                g.setFixedSize(28, 28)
                g.setProperty("class", "btn-gear")
                g.setToolTip("高级选项")
                g.setCursor(Qt.PointingHandCursor)
                g.clicked.connect(gear)
                r.addWidget(g)
            return r

        form.addLayout(_row(self.btn_queue, self._open_queue_config))
        form.addLayout(_row(self.btn_accept, self._open_accept_config))
        form.addLayout(_row(self.btn_ban, self._open_ban_config))
        form.addLayout(_row(self.btn_pick, self._open_pick_config))

        layout.addWidget(box)

        # 一键连招：总开关（不持久化，默认关）+ [管理连招…] 弹窗（ComboPanel 装进 QDialog）
        if self._combo_manager is not None:
            combo_row = QHBoxLayout()
            self.btn_combo_enable = ToggleButton("连招总开关", False)
            self.btn_combo_enable.setMinimumWidth(180)
            self.btn_combo_enable.toggled_state.connect(self._combo_manager.set_enabled)
            combo_row.addWidget(self.btn_combo_enable)
            self.btn_combo_manage = QPushButton("管理连招…")
            self.btn_combo_manage.clicked.connect(self._open_combo_manager)
            combo_row.addWidget(self.btn_combo_manage)
            combo_row.addStretch()
            layout.addLayout(combo_row)

        layout.addStretch()

        # 绑定回调
        if service is not None:
            self.btn_queue.toggled_state.connect(
                lambda on: _safe(service, "set_auto_queue", on))
            self.btn_accept.toggled_state.connect(
                lambda on: _safe(service, "set_auto_accept", on))
            self.btn_ban.toggled_state.connect(
                lambda on: _safe(service, "set_auto_ban", on))
            self.btn_pick.toggled_state.connect(
                lambda on: _safe(service, "set_auto_pick", on))

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)

    def _open_combo_manager(self):
        """打开连招管理弹窗（非模态，关闭即销毁，ComboPanel 原样装入）。"""
        from app.ui.dialogs.combo_manager_dialog import ComboManagerDialog
        dlg = ComboManagerDialog(self._combo_manager, self)
        dlg.setAttribute(Qt.WA_DeleteOnClose)
        dlg.show()

    def _open_queue_config(self):
        if self._svc is None:
            return
        cfg = getattr(self._svc, "config", None)
        cur = {}
        if cfg is not None:
            cur = {
                "auto_queue_mode": getattr(cfg, "auto_queue_mode", "normal"),
                "auto_queue_primary_role": getattr(cfg, "auto_queue_primary_role", "不限"),
                "auto_queue_secondary_role": getattr(cfg, "auto_queue_secondary_role", "不限"),
            }
        dlg = AutoQueueConfigDialog(cur, self)
        if dlg.exec():
            vals = dlg.values()
            for k, v in vals.items():
                try:
                    setattr(cfg, k, v)
                except Exception:
                    pass
            print(f"[UI] auto_queue config saved: {vals}")
            try:
                import yaml
                path = "config/settings.yaml"
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                data.setdefault("automation", {}).update(vals)
                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            except Exception as e:
                print(f"[UI] save yaml failed: {e}")

    def _open_accept_config(self):
        """延迟接受配置：写入 automation.accept_delay（ready_check_controller 已消费）。"""
        if self._svc is None:
            return
        cfg = getattr(self._svc, "config", None)
        cur = float(getattr(cfg, "accept_delay", 0.0) or 0.0)
        from PySide6.QtWidgets import QInputDialog
        val, ok = QInputDialog.getDouble(self, "延迟接受配置",
                                         "延迟接受（秒，0=立即接受）：",
                                         cur, 0.0, 30.0, 1)
        if not ok:
            return
        try:
            setattr(cfg, "accept_delay", val)
            # 同时保存到yaml配置（沿用现有模式）
            import yaml
            path = "config/settings.yaml"
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            data.setdefault("automation", {})["accept_delay"] = val
            with open(path, "w", encoding="utf-8") as f:
                yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
            print(f"[UI] accept_delay saved: {val}")
        except Exception as e:
            print(f"[UI] save accept_delay failed: {e}")

    def _open_ban_config(self):
        self._open_banpick("自动禁用配置", "ban_priority")

    def _open_pick_config(self):
        self._open_banpick("自动选择配置", "pick_priority")

    def _open_banpick(self, title, field):
        if self._svc is None:
            return
        cfg = getattr(self._svc, "config", None)
        cur = list(getattr(cfg, field, []) or [])
        dlg = BanPickConfigDialog(title, cur, self)
        if dlg.exec():
            try:
                setattr(cfg, field, dlg.values())
                # 同时保存到yaml配置
                import yaml
                path = "config/settings.yaml"
                with open(path, "r", encoding="utf-8") as f:
                    data = yaml.safe_load(f) or {}
                data.setdefault("automation", {})[field] = dlg.values()
                with open(path, "w", encoding="utf-8") as f:
                    yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
                print(f"[UI] {field} saved: {dlg.values()}")
            except Exception as e:
                print(f"[UI] save {field} failed: {e}")

    def refresh(self):
        try:
            # 优先从 LCU 直接读取 gameflow（automation 禁用时也能更新）
            phase = "?"
            if self._lcu is not None:
                try:
                    phase = self._lcu.get_gameflow_phase() or "?"
                except Exception:
                    pass
            # fallback 到 automation service
            if phase == "?" and self._svc is not None:
                phase = getattr(self._svc, "_last_phase", "?")
            
            # gameflow 中文映射
            _PHASE_ZH = {
                "None": "未开始", "Lobby": "大厅",
                "ChampSelect": "英雄选择", "GameStart": "游戏开始",
                "InProgress": "游戏中", "WaitingForStats": "等待结算",
                "EndOfGame": "已结束",
            }
            phase_zh = _PHASE_ZH.get(phase, phase)
            
            state = "?"
            if self._svc is not None and hasattr(self._svc, "_state"):
                state = self._svc._state.value
            _STATE_ZH = {"idle":"空闲","in_lobby":"在大厅","queue_searching":"排队中",
                         "ready_check":"确认对局","champ_select":"英雄选择","in_game":"游戏中",
                         "settlement":"结算中","safe_stop":"已停止"}
            state_zh = _STATE_ZH.get(state, state)
            self.lbl_state.setText(f"状态：{state_zh}")
            self.lbl_phase.setText(f"游戏阶段：{phase_zh}")
            try:
                hint = getattr(self._svc, "get_status_hint", None)
                self._lbl_hint.setText(hint() if hint else "")
            except Exception:
                pass
            cfg = getattr(self._svc, "config", None)
            if cfg is not None:
                self.btn_queue.set_state(bool(getattr(cfg, "auto_queue", False)))
                self.btn_accept.set_state(bool(getattr(cfg, "auto_accept", False)))
                self.btn_ban.set_state(bool(getattr(cfg, "auto_ban", False)))
                self.btn_pick.set_state(bool(getattr(cfg, "auto_pick", False)))
        except Exception:
            pass


def _safe(obj, method, *args):
    fn = getattr(obj, method, None)
    if fn is not None:
        try:
            fn(*args)
        except Exception:
            pass
