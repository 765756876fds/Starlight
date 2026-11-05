"""状态栏（Glass 重绘）：实时读服务状态。

- LCU / ASR：Lucide 线性图标（link / cpu）+ 状态文字，连接正常绿色发光、
  异常红色暗淡、待机次色——取代原生圆点 ● 与 RichText 拼色；
- 语音开关：可点击玻璃胶囊按钮（mic / mic-off），图标随状态切换；
- 半透明底 + 极细上边框由全局 QSS（applier #QStatusBar）提供，与主窗口融合。
"""
from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QStatusBar, QWidget

from ..theme.colors import Colors
from ..theme.tokens import Tokens
from ..widgets.icon_utils import lucide_icon
from ...core.notifications import get_notification_center, notify
from ..widgets.notification_stream import NotificationStream
from ..widgets.voice_island import VoiceIsland



class StatusBar(QStatusBar):
    notification_requested = Signal()

    def __init__(self, services=None, parent=None):
        super().__init__(parent)
        self.setSizeGripEnabled(False)
        self._svc = services or {}
        self._last_tts_failure = None
        self._last_lcu_connected = None
        self._notices = get_notification_center()

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
        self.island = VoiceIsland(self)
        self.btn_voice_feedback = self.island  # Legacy attribute; no extra window or label.
        self.island.open_notifications.connect(self.notification_requested)
        self._notice_stream = NotificationStream(self._notices, self)
        self._notice_stream.changed.connect(self._refresh_island, Qt.QueuedConnection)

        lay = QHBoxLayout()
        lay.setContentsMargins(Tokens.SPACING_SM, 4, Tokens.SPACING_SM, 4)
        lay.setSpacing(Tokens.SPACING_MD)   # 控件间拉开距离（8px 网格 2 格）
        lay.addWidget(self.lbl_lcu)
        lay.addWidget(self.lbl_asr)
        lay.addWidget(self.btn_mic)
        lay.addWidget(self.btn_voice_feedback)
        lay.addStretch()
        holder = QWidget()
        holder.setLayout(lay)
        self.addWidget(holder, 1)
        # QStatusBar's default height can squeeze a two-line custom child.
        self.setMinimumHeight(self.island.maximumHeight() + 2 * Tokens.SPACING_SM)

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

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.lbl_lcu.setVisible(self.width() >= 620)
        self.lbl_asr.setVisible(self.width() >= 460)

    # ---- 图标 + 状态色渲染 ----
    @staticmethod
    def _update_state(lbl: QLabel, icon_name: str, ok: bool, label: str):
        color = Tokens.SUCCESS if ok else Tokens.DANGER
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
            self.lbl_lcu.setStyleSheet(f"color: {Tokens.TEXT_SECONDARY};")
        self.lbl_lcu.setToolTip("英雄联盟客户端连接状态；未启动游戏不影响音乐和电影")
        if lcu is not None and lcu_ok != self._last_lcu_connected:
            if lcu_ok or self._last_lcu_connected is not None:
                notify("游戏客户端已连接" if lcu_ok else "游戏客户端连接已断开",
                       "可以使用客户端相关功能" if lcu_ok else "客户端重新打开后会自动尝试连接，不影响音乐和电影",
                       status="success" if lcu_ok else "warning", source="game",
                       dedup_key=f"lcu-connected-{lcu_ok}")
            self._last_lcu_connected = lcu_ok

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
        self._refresh_island()
        try:
            on = bool(voice.is_mic_on()) if voice is not None else False
            self._render_mic_btn(on)
            self.btn_mic.setChecked(on)
            self.btn_mic.setEnabled(voice is not None)
        except Exception:
            self.btn_mic.setEnabled(False)
            self.btn_mic.setToolTip("暂时无法读取语音状态，请查看设置 → 诊断")

        # refresh 由 GUI 定时器调用；后台失败只在这里显示，不跨线程操作控件。
        try:
            provider = getattr(voice, "tts_provider", None)
            tts = provider() if callable(provider) else None
            state = tts.get_state() if tts is not None else None
            get_failure = getattr(tts, "get_last_failure", None)
            last = get_failure() if callable(get_failure) else None
            if not isinstance(last, tuple) or len(last) != 2:
                last = None
            if last is None and not callable(get_failure) and getattr(state, "value", None) == "failed":
                last = (getattr(tts, "_generation", None),
                        "语音播报失败，请检查网络或音频设备；操作结果请以界面状态为准")
            if last is not None:
                generation, message = last
                failure = (id(tts), generation)
                if failure != self._last_tts_failure:
                    self._last_tts_failure = failure
                    operation_id = getattr(tts, "_last_failure_notification_id", getattr(tts, "_notification_id", ""))
                    if isinstance(operation_id, str) and operation_id:
                        self._notices.update(operation_id, speech_status="failed")
                    else:
                        notify("语音播报未成功", "请检查网络、语音配置或播放设备",
                               status="warning", source="speech", dedup_key="tts-failed")
        except Exception:
            pass

    def _read_voice_activity(self):
        voice = self._svc.get("voice")
        getter = getattr(voice, "get_activity", None)
        try:
            activity = getter() if callable(getter) else {}
            return activity if isinstance(activity, dict) else {}
        except Exception:
            return {}

    @Slot()
    def _refresh_island(self):
        voice = self._svc.get("voice")
        listening = bool(voice.is_listening()) if voice is not None and callable(getattr(voice, "is_listening", None)) else False
        self.island.set_current(self._notices.get(self._notices.current_voice_id), listening)

    def _show_voice_feedback(self):
        self.notification_requested.emit()

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
                                          14, Tokens.SUCCESS if on else Tokens.TEXT_SECONDARY))
        self.btn_mic.setToolTip("语音识别开关（点击切换）")
