"""Compatibility import: the former console tab is the user notification center.

Technical stdout/stderr and structured logging remain in the original log files.
This panel deliberately does not subscribe to LogBus.
"""
from .notification_panel import NotificationPanel


class ConsolePanel(NotificationPanel):
    pass
