"""状态栏（Glass 重绘）：实时读服务状态。

- LCU / ASR：Lucide 线性图标（link / cpu）+ 状态文字，连接正常绿色发光、
  异常红色暗淡、待机次色——取代原生圆点 ● 与 RichText 拼色；
- 语音开关：可点击玻璃胶囊按钮（mic / mic-off），图标随状态切换；
- 半透明底 + 极细上边框由全局 QSS（applier #QStatusBar）提供，与主窗口融合。
"""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStatusBar, QWidget

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon

_OK = "#4ade80"      # 绿亮（已连接）
_BAD = "#ef4444"     # 红暗（异常）
_IDLE = Colors.TEXT_SECONDARY   # 待机（次色，WCAG 达标）


class StatusBar(QStatusBar):
    def __init__(self, services=None, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self._svc = services or {}

        self.lbl_lcu = QLabel()
        self.lbl_lcu.setProperty("class", "status_item")
        self.lbl_asr = QLabel()
        self.lbl_asr.setProperty("class", "status_item")
        self.btn_mic = QPushButton()
        self.btn_mic.setObjectName("btn_status_mic")
        self.btn_mic.setCheckable(True)
        self.btn_mic.setCursor(Qt.PointingHandCursor)
        self.btn_mic.clicked.connect(self._toggle_mic)
        self._render_mic_btn(False)

        lay = QHBoxLayout()
        lay.setContentsMargins(Tokens.SPACING_SM, 4, Tokens.SPACING_SM, 4)
        lay.setSpacing(Tokens.SPACING_MD)   # 控件间拉开距离（8px 网格 2 格）
        lay.addWidget(self.lbl_lcu)
        lay.addWidget(self.lbl_asr)
        lay.addWidget(self.btn_mic)
        lay.addStretch()
        holder = QWidget()
        holder.setLayout(lay)
        self.addWidget(holder, 1)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(1000)
        self.refresh()

    def showEvent(self, event):
        super().showEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        if hasattr(self, "_timer") and self._timer is not None:
            self._timer.stop()

    # ---- 图标 + 状态色渲染 ----
    @staticmethod
    def _update_state(lbl: QLabel, icon_name: str, ok: bool, label: str):
        color = _OK if ok else _BAD
        icon = lucide_icon(icon_name, 14, color)
        lbl.setPixmap(icon.pixmap(14, 14))
        lbl.setText(f" {label}")
        lbl.setStyleSheet(f"color: {color};")

    def refresh(self):
        lcu = self._svc.get("lcu")
        lcu_ok = False
        if lcu is not None:
            lcu_ok = bool(getattr(lcu, "connected", False))
        self._update_state(self.lbl_lcu, "link", lcu_ok,
                           "游戏已连接" if lcu_ok else "等待游戏客户端")
        if not lcu_ok:
            self.lbl_lcu.setStyleSheet(f"color: {_IDLE};")
        self.lbl_lcu.setToolTip("英雄联盟客户端连接状态；未启动游戏不影响音乐和电影")

        asr = self._svc.get("asr")
        asr_label = "未就绪"
        asr_ok = False
        if asr is not None:
            if hasattr(asr, "is_available") and asr.is_available():
                asr_ok = True
                engines = getattr(asr, "engines", []) or []
                names = [getattr(e, "name", type(e).__name__) for e in engines]
                asr_label = "/".join(names) if names else "READY"
        self._update_state(self.lbl_asr, "cpu", asr_ok, f"ASR {asr_label}")
        self.lbl_asr.setText("语音识别就绪" if asr_ok else "语音识别未就绪")
        self.lbl_asr.setToolTip(f"识别引擎：{asr_label}" if asr_ok else "请在设置 → 诊断中检查运行环境")
        voice = self._svc.get("voice")
        try:
            on = bool(voice.is_mic_on()) if voice is not None else False
            self._render_mic_btn(on)
            self.btn_mic.setChecked(on)
            self.btn_mic.setEnabled(voice is not None)
        except Exception:
            self.btn_mic.setEnabled(False)
            self.btn_mic.setToolTip("暂时无法读取语音状态，请查看设置 → 诊断")

    def _toggle_mic(self):
        try:
            voice = self._svc.get("voice")
            if voice:
                on = voice.toggle_mic()
                self._render_mic_btn(on)
        except Exception as e:
            print(f"麦克风开关错误: {e}")
            self.showMessage("语音开关未成功，请查看设置 → 诊断", 5000)
        self.refresh()

    def _render_mic_btn(self, on: bool):
        self.btn_mic.setText("语音开" if on else "语音关")
        self.btn_mic.setIcon(lucide_icon("mic" if on else "mic-off",
                                          14, _OK if on else _IDLE))
        self.btn_mic.setToolTip("语音识别开关（点击切换）")
