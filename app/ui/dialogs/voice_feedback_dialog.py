"""最近一次语音交互的只读视图，不创建语音服务或执行业务命令。"""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel

from ..widgets.glass_dialog import GlassDialog


SPEECH_LABELS = {
    "none": "尚无播报",
    "unavailable": "播报服务不可用，执行反馈仍可查看",
    "pending": "等待播报",
    "playing": "正在合成或播报",
    "finished": "播报已结束",
    "failed": "播报失败；业务操作不会因此重试",
    "cancelled": "播报已取消或被新指令替代",
    "expired": "反馈已过期，不再播报",
    "rejected": "未进入播报队列，请以执行反馈为准",
    "unknown": "无法确认播报状态",
}


class VoiceFeedbackDialog(GlassDialog):
    def __init__(self, parent=None):
        super().__init__("语音反馈", parent)
        self.setModal(False)
        self.resize(500, 340)
        self.setMinimumWidth(380)
        self.fields = {}
        for key, title in (("heard", "听到的内容"), ("feedback", "执行反馈"),
                           ("speech_status", "语音播报")):
            heading = QLabel(title)
            heading.setStyleSheet("font-weight: 600;")
            self.body_layout.addWidget(heading)
            value = QLabel()
            value.setTextFormat(Qt.PlainText)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            value.setWordWrap(True)
            self.body_layout.addWidget(value)
            self.fields[key] = value
        hint = QLabel("可以试试：音乐暂停、下一首、音乐音量五十、电影暂停。\n"
                      "没有声音时先看执行反馈；不必重复下达已经完成的命令。")
        hint.setWordWrap(True)
        self.body_layout.addWidget(hint)
        self.set_activity({})

    def set_activity(self, activity):
        self.fields["heard"].setText(activity.get("heard") or "尚未收到识别文本")
        feedback = activity.get("feedback")
        if not feedback:
            feedback = ("已分发，尚未收到结果确认" if activity.get("intent")
                        else "开启底部语音开关，说完一句后可在这里查看反馈")
        self.fields["feedback"].setText(feedback)
        self.fields["speech_status"].setText(
            SPEECH_LABELS.get(activity.get("speech_status", "none"), SPEECH_LABELS["unknown"]))
