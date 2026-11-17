"""Process-only experimental switch; queued metadata, never player/SQL polling."""

import weakref

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QCheckBox, QGroupBox, QLabel, QVBoxLayout


class ListenMusicTrialControls(QGroupBox):
    _changed = Signal(object)

    def __init__(self, trial=None, parent=None):
        super().__init__("实验功能：智能听歌决策", parent)
        self._trial = trial
        layout = QVBoxLayout(self)
        self.toggle = QCheckBox("本次运行启用（仅“我想听歌了”）")
        self.toggle.setEnabled(trial is not None)
        layout.addWidget(self.toggle)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.details)
        hint = QLabel("默认关闭，重启后恢复旧模式。首次试用不自动选择本地收藏。"
                      "开关关闭不会撤销已发送的播放操作；未决结果继续受保护。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self._changed.connect(self._refresh, Qt.QueuedConnection)
        self.toggle.toggled.connect(self._toggle)
        if trial is not None:
            ref = weakref.ref(self)

            def enqueue(state):
                widget = ref()
                if widget is not None:
                    widget._changed.emit(state)

            trial.set_listener(enqueue)
            self.destroyed.connect(lambda *_: trial.set_listener(None))
        self._refresh()

    def _toggle(self, enabled):
        if self._trial is not None:
            self._trial.set_enabled(enabled)
            self._refresh()

    def _refresh(self, _state=None):
        if self._trial is None:
            self.status.setText("试验入口尚未就绪，仍使用原有听歌模式。")
            self.details.clear()
            return
        # Read current metadata at delivery, not a possibly older queued payload.
        state = self._trial.snapshot()
        self.toggle.blockSignals(True)
        try:
            self.toggle.setChecked(state.enabled)
        finally:
            self.toggle.blockSignals(False)
        if state.active:
            label = "正在执行或观察播放请求，请稍候。关闭开关仅影响后续请求。"
        elif state.protected:
            label = ("请求已受理，尚未确认起播；该入口后续自动播放受保护。"
                     if state.receipt == "accepted" else
                     "上次播放副作用尚未确认；该入口后续自动播放受保护，请查看音乐页面。")
        elif state.enabled:
            label = "B5 试验已开启：保持播放、恢复暂停，或在准入前使用原推荐器。"
        else:
            label = "V1 默认模式：后续新请求使用原推荐器。"
        self.status.setText(label)
        self.details.setText(f"最近路由：{state.route}　方案：{state.plan}　回执：{state.receipt}\n"
                             f"操作编号：{state.operation_id or '尚无请求'}")
