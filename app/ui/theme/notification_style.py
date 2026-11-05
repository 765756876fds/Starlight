"""Shared semantic palette for notifications and the voice capsule."""
from dataclasses import dataclass
from .tokens import Tokens
from .colors import palette_for


@dataclass(frozen=True)
class NotificationStyle:
    background: str
    surface: str
    border: str
    text: str
    secondary: str
    success: str
    warning: str
    failed: str
    processing: str

    def color(self, status):
        return getattr(self, status, self.warning)


def notification_style(theme=None):
    colors = palette_for(theme)
    return NotificationStyle(colors.panel_background, colors.elevated_background, colors.border,
                             colors.primary_text, colors.secondary_text, colors.success,
                             colors.warning, colors.error, colors.accent)


STATUS_LABELS = {"processing": "处理中", "success": "已完成", "warning": "提醒", "failed": "未完成"}


def status_icon(status):
    return {"success": "check", "failed": "circle-alert", "warning": "info"}.get(status, "mic")


SPEECH_NOTES = {
    "disabled": "语音播报未开启，操作结果保留",
    "failed": "语音播报未成功，请检查网络或播放设备",
    "unavailable": "语音播报暂不可用，操作结果仍可查看",
    "rejected": "本次未能播报，操作结果仍可查看",
    "cancelled": "播报已取消或被新指令替代",
    "expired": "播报已过期，操作结果保留",
}
