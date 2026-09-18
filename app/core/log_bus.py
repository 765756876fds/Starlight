from PySide6.QtCore import QObject, Signal


class LogBus(QObject):
    sig_log = Signal(str)


_instance = None


def get_log_bus() -> LogBus:
    global _instance
    if _instance is None:
        _instance = LogBus()
    return _instance
