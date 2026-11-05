"""Shared semantic palette for notifications and the voice capsule."""
from dataclasses import dataclass
from .tokens import Tokens


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


def notification_style(theme="dark"):
    if theme == "light":
        return NotificationStyle("#f5f7fb", "#ffffff", "#dce2ea", "#172033",
                                 "#526078", "#137847", "#946000", "#bd303f", "#2469c8")
    return NotificationStyle(Tokens.BG_BASE, Tokens.BG_ELEVATED, Tokens.BORDER,
                             Tokens.TEXT_PRIMARY, Tokens.TEXT_SECONDARY,
                             Tokens.SUCCESS, Tokens.WARNING, Tokens.DANGER, Tokens.ACCENT)


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
