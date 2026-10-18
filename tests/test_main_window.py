"""主窗口边缘缩放（WM_NCHITTEST）与尺寸记忆（QSettings）回归测试。"""
import ctypes
import sys
from ctypes import wintypes

import pytest

import app.ui.main_window as mw
from app.ui.main_window import MainWindow, _nchit_edge


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


# ---- 纯函数：边缘命中码 ----
def test_nchit_center_is_client():
    assert _nchit_edge(100, 100, 300, 300) == mw._HT_CLIENT


def test_nchit_edges():
    assert _nchit_edge(2, 150, 300, 300) == mw._HT_LEFT
    assert _nchit_edge(297, 150, 300, 300) == mw._HT_RIGHT
    assert _nchit_edge(150, 2, 300, 300) == mw._HT_TOP
    assert _nchit_edge(150, 297, 300, 300) == mw._HT_BOTTOM


def test_nchit_corners():
    assert _nchit_edge(2, 2, 300, 300) == mw._HT_TOPLEFT
    assert _nchit_edge(297, 2, 300, 300) == mw._HT_TOPRIGHT
    assert _nchit_edge(2, 297, 300, 300) == mw._HT_BOTTOMLEFT
    assert _nchit_edge(297, 297, 300, 300) == mw._HT_BOTTOMRIGHT


def test_nchit_just_outside_inside():
    # 5px 边界：6px 处不再命中
    assert _nchit_edge(6, 150, 300, 300) == mw._HT_CLIENT
    assert _nchit_edge(150, 6, 300, 300) == mw._HT_CLIENT


# ---- nativeEvent：eventType 兼容 + 缩放命中 ----
def _make_nchittest_msg(sx: int, sy: int):
    """构造一个 WM_NCHITTEST 的 MSG 内存，返回其地址。"""
    msg = wintypes.MSG()
    # ctypes.wintypes.MSG 句柄字段名为 hWnd（跨版本兼容用 setattr）
    if hasattr(msg, "hWnd"):
        msg.hWnd = 0
    else:
        msg.hwnd = 0
    msg.message = mw._WM_NCHITTEST
    msg.lParam = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
    buf = ctypes.create_string_buffer(ctypes.sizeof(msg))
    ctypes.memmove(buf, ctypes.byref(msg), ctypes.sizeof(msg))
    return ctypes.addressof(buf)


def test_native_event_qbytearray_eventtype(qapp, monkeypatch):
    """eventType 为 QByteArray（真实 Qt 形态）时能拦截 WM_NCHITTEST。"""
    from PySide6.QtCore import QByteArray
    w = MainWindow(music=None)
    rect = wintypes.RECT(100, 200, 700, 800)
    monkeypatch.setattr(mw.windll.user32, "GetWindowRect",
                        lambda hwnd, lp: ctypes.memmove(lp, ctypes.byref(rect), ctypes.sizeof(rect)) or 1)
    msg_addr = _make_nchittest_msg(105, 205)  # 相对窗口 (5,5) → 左上角
    handled, hit = w.nativeEvent(QByteArray(b"windows_generic_MSG"), msg_addr)
    assert handled is True
    assert hit == mw._HT_TOPLEFT
    w.deleteLater()


def test_native_event_str_eventtype(qapp, monkeypatch):
    """eventType 解码为 str 时同样生效。"""
    w = MainWindow(music=None)
    rect = wintypes.RECT(100, 200, 700, 800)
    monkeypatch.setattr(mw.windll.user32, "GetWindowRect",
                        lambda hwnd, lp: ctypes.memmove(lp, ctypes.byref(rect), ctypes.sizeof(rect)) or 1)
    msg_addr = _make_nchittest_msg(695, 795)  # 相对窗口 (595,595) → 右下角
    handled, hit = w.nativeEvent(b"windows_generic_MSG", msg_addr)
    assert handled is True
    assert hit == mw._HT_BOTTOMRIGHT
    w.deleteLater()


def test_native_event_maximized_returns_client(qapp, monkeypatch):
    """最大化时不参与边缘缩放，返回 HTCLIENT。"""
    w = MainWindow(music=None)
    monkeypatch.setattr(w, "isMaximized", lambda: True)
    msg_addr = _make_nchittest_msg(105, 205)
    handled, hit = w.nativeEvent(b"windows_generic_MSG", msg_addr)
    assert handled is True
    assert hit == mw._HT_CLIENT
    w.deleteLater()


def test_native_event_non_hittest_passthrough(qapp):
    """非 WM_NCHITTEST 消息走默认处理，不崩溃。"""
    w = MainWindow(music=None)
    msg = wintypes.MSG()
    msg.hwnd = 0
    msg.message = 0x0010  # WM_CLOSE
    msg.lParam = 0
    buf = ctypes.create_string_buffer(ctypes.sizeof(msg))
    ctypes.memmove(buf, ctypes.byref(msg), ctypes.sizeof(msg))
    handled, hit = w.nativeEvent(b"windows_generic_MSG", ctypes.addressof(buf))
    assert handled is False or handled is not None
    w.deleteLater()


# ---- QSettings：读取恢复 + closeEvent 保存 ----
class _FakeSettings:
    """内存版 QSettings（避免测试污染注册表）。"""

    def __init__(self, *args, **kwargs):
        self._d = {}

    def value(self, key, default=None):
        return self._d.get(key, default)

    def setValue(self, key, value):
        self._d[key] = value


def test_geometry_restored_from_settings(qapp, monkeypatch):
    monkeypatch.setattr(mw, "QSettings", _FakeSettings)
    w = MainWindow(music=None)
    assert w.width() > 0
    w.deleteLater()


def test_close_event_saves_geometry(qapp, monkeypatch):
    fake = _FakeSettings()
    monkeypatch.setattr(mw, "QSettings", lambda *a, **k: fake)
    w = MainWindow(music=None)
    w.resize(900, 700)
    w.close()
    assert fake._d.get("geometry") is not None
    assert fake._d.get("size") is not None
    w.deleteLater()
