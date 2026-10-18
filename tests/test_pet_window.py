"""PetWindow（GIF 版）测试：无边框置顶透明 + QMovie + 拖拽 + 贴边隐藏 + 全屏检测。"""
import os
import sys
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.widgets.pet_window import (
    PetWindow,
    _EDGE_SNAP,
    _is_fullscreen_window,
)


@pytest.fixture(scope="session")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def pet(qapp):
    w = PetWindow()
    yield w
    w.close()
    w.deleteLater()


# ---- 窗口属性 ----
def test_pet_window_flags(pet):
    flags = pet.windowFlags()
    assert flags & Qt.FramelessWindowHint
    assert flags & Qt.Tool
    assert flags & Qt.WindowStaysOnTopHint
    assert pet.testAttribute(Qt.WA_TranslucentBackground)


def test_pet_window_has_label_movie(pet):
    assert pet._label is not None
    assert pet._label.parent() is pet
    # 素材存在时 QMovie 已启动
    assert pet._movie is not None
    assert pet._current == "idle"


def test_pet_motion_fallback_missing_file(qapp):
    """缺少 listen.gif 时 play_motion 回退 idle，不崩溃。"""
    w = PetWindow(motions={"idle": str(Path(__file__).parent / "data" / "idle.gif"),
                           "listen": "Z:/not_exist/listen.gif",
                           "ok": "Z:/not_exist/ok.gif"})
    w.play_motion("listen")
    assert w._current == "idle"
    w.close()
    w.deleteLater()


def test_pet_motion_switch(qapp, tmp_path):
    """提供自定义素材目录时，可正常切换到 listen。"""
    src = Path(__file__).parent / "data" / "idle.gif"
    idle = tmp_path / "idle.gif"
    listen = tmp_path / "listen.gif"
    idle.write_bytes(src.read_bytes())
    listen.write_bytes(src.read_bytes())
    w = PetWindow(motions={"idle": str(idle), "listen": str(listen), "ok": str(listen)})
    w.play_motion("listen")
    assert w._current == "listen"
    w.close()
    w.deleteLater()


# ---- 拖拽 ----
def test_pet_drag_moves_window(pet, qapp):
    pet.resize(240, 360)
    pet.show()
    qapp.processEvents()
    pet.move(100, 100)
    start = pet.frameGeometry().topLeft()
    QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(30, 30))
    QTest.mouseMove(pet, QPoint(80, 60))
    QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(80, 60))
    qapp.processEvents()
    # 远离屏幕边缘（100,100 起步），不应触发贴边隐藏
    assert pet.frameGeometry().topLeft() == start + QPoint(50, 30)
    assert pet._drag_offset is None
    assert pet.isVisible()
    pet.hide()


# ---- 贴边隐藏（松手判定） ----
def test_pet_snap_hide_near_left_edge(pet, qapp):
    pet.resize(240, 360)
    scr = pet.screen() or QApplication.primaryScreen()
    geo = scr.availableGeometry()
    pet.move(geo.left() + 5, geo.top() + 100)   # 距左缘 5px < 15px
    pet.show()
    qapp.processEvents()
    pet._snap_to_edge_if_near()
    assert not pet.isVisible()
    assert pet._hidden_by_edge


def test_pet_snap_no_hide_far_from_edge(pet, qapp):
    scr = pet.screen() or QApplication.primaryScreen()
    geo = scr.availableGeometry()
    pet.move(geo.left() + 300, geo.top() + 300)  # 远离边缘
    pet.show()
    qapp.processEvents()
    pet._snap_to_edge_if_near()
    assert pet.isVisible()
    assert not pet._hidden_by_edge


# ---- 全屏检测 ----
def test_pet_fullscreen_hide_and_restore(pet, qapp, monkeypatch):
    import app.ui.widgets.pet_window as pw
    pet.show()
    qapp.processEvents()
    monkeypatch.setattr(pw, "_is_fullscreen_window", lambda hwnd: True)
    pet._check_fullscreen()
    assert not pet.isVisible()
    assert pet._hidden_by_fullscreen
    monkeypatch.setattr(pw, "_is_fullscreen_window", lambda hwnd: False)
    pet._check_fullscreen()
    assert pet.isVisible()
    assert not pet._hidden_by_fullscreen
    pet.hide()


def test_pet_fullscreen_ignores_self(pet, qapp, monkeypatch):
    import app.ui.widgets.pet_window as pw
    pet.show()
    qapp.processEvents()
    monkeypatch.setattr(pw, "_is_fullscreen_window", lambda hwnd: True)
    monkeypatch.setattr(pw.ctypes.windll.user32, "GetForegroundWindow", lambda: int(pet.winId()))
    pet._check_fullscreen()
    assert pet.isVisible()


def test_is_fullscreen_window_size_heuristic():
    """非 Windows 平台启发式判定（宽高接近屏幕 → True）。"""
    if sys.platform.startswith("win"):
        pytest.skip("windows 走 Win32 分支")
