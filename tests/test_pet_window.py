"""桌面宠物第一阶段：PetWindow 窗口属性 / 拖拽 / 贴边隐藏 / 设置开关持久化 / MainTabs 集成。
注意：不触发真实 QOpenGLWidget GL 上下文（offscreen 无 GL 会段错误），GL 用普通 QWidget 替换。"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QWidget

from app.ui.widgets.pet_window import PetWindow, PetGLWidget, _EDGE_MARGIN, _PEEK_WIDTH


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def pet(qapp, monkeypatch):
    # GL 承载替换为普通 QWidget：避免 offscreen 无 GL 上下文触发 initializeGL 崩溃
    monkeypatch.setattr("app.ui.widgets.pet_window.PetGLWidget",
                        lambda model_path, parent=None: QWidget(parent))
    return PetWindow(model_path="dummy.model3.json")


def test_pet_window_flags(pet):
    assert bool(pet.windowFlags() & Qt.FramelessWindowHint)
    assert bool(pet.windowFlags() & Qt.Tool)
    assert bool(pet.windowFlags() & Qt.WindowStaysOnTopHint)
    assert pet.testAttribute(Qt.WA_TranslucentBackground)
    assert pet.width() == 240 and pet.height() == 360


def test_pet_gl_uses_live2d_api(qapp, monkeypatch):
    """PetGLWidget 的 live2d 调用序列（mock 库）：init → LoadModelJson → CreateRenderer → 待机。"""
    import types
    from app.ui.widgets import pet_window
    calls = []

    class FakeL2D:
        @staticmethod
        def init():
            calls.append("init")

        @staticmethod
        def clearBuffer(*a):
            pass

        class Model:
            def __init__(self):
                self.motion_calls = []

            def LoadModelJson(self, p):
                calls.append(f"load:{p}")

            def CreateRenderer(self):
                calls.append("renderer")

            def SetAutoBlink(self, v):
                pass

            def SetAutoBreath(self, v):
                pass

            def StartMotion(self, g, n, pr):
                calls.append(f"motion:{g}:{n}")

            def Update(self):
                calls.append("update")

            def Draw(self):
                calls.append("draw")

        class MotionPriority:
            Normal = 0

    fake = types.SimpleNamespace(**{k: getattr(FakeL2D, k) for k in
                                    ("init", "clearBuffer", "Model", "MotionPriority")})
    fake.v2 = fake  # 模拟 v2 子模块：真机走 v2 纯 Python 路径
    monkeypatch.setitem(sys.modules, "live2d", fake)
    gl = PetGLWidget("x/y.model3.json")
    gl.initializeGL()
    assert "init" in calls and "load:x/y.model3.json" in calls and "renderer" in calls
    assert "motion:idle:0" in calls
    gl.paintGL()
    assert "update" in calls and "draw" in calls


def test_pet_drag_moves_window(pet, qapp, monkeypatch):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    pet._gl = QWidget(pet)          # 无 GL 承载
    pet.resize(240, 360)
    pet.show()
    qapp.processEvents()
    pet.move(100, 100)
    start = pet.frameGeometry().topLeft()
    QTest.mousePress(pet, Qt.LeftButton, pos=QPoint(30, 30))
    QTest.mouseMove(pet, QPoint(80, 60))
    QTest.mouseRelease(pet, Qt.LeftButton, pos=QPoint(80, 60))
    qapp.processEvents()
    # 拖拽距离 = (80-30, 60-30) = (50, 30)
    assert pet.frameGeometry().topLeft() == start + QPoint(50, 30)
    assert pet._drag_offset is None
    pet.hide()


def test_pet_edge_hide_target_pos(pet):
    from PySide6.QtCore import QRect
    geo = QRect(0, 0, 1920, 1080)
    pet._hidden_edge = "left"
    x, y = pet._target_hidden_pos(geo)
    assert x == 0 - pet.width() + _PEEK_WIDTH and y == pet.y()
    pet._hidden_edge = "right"
    x, _ = pet._target_hidden_pos(geo)
    assert x == geo.right() - _PEEK_WIDTH
    pet._hidden_edge = "top"
    _, y = pet._target_hidden_pos(geo)
    assert y == 0 - pet.height() + _PEEK_WIDTH


def test_pet_edge_hide_and_restore(pet, qapp, monkeypatch):
    """贴近屏幕左缘 + 鼠标不在窗口上 → 隐藏留触发条；鼠标进触发条 → 恢复。"""
    pet._gl = QWidget(pet)
    pet.resize(240, 360)
    pet.show()
    qapp.processEvents()
    scr = pet.screen() or QApplication.primaryScreen()
    geo = scr.availableGeometry()
    pet.move(geo.left() + 5, 400)          # 贴近左缘
    normal = (pet.x(), pet.y())
    # 鼠标移到屏幕中部（不在窗口/触发条上）
    pet.cursor().setPos(geo.left() + 500, 400)
    pet._check_edge()
    assert pet._hidden_edge == "left"
    assert pet.x() == 0 - pet.width() + _PEEK_WIDTH, "隐藏后只留触发条"
    # 鼠标移到触发条内 → 恢复
    pet.cursor().setPos(geo.left() + _PEEK_WIDTH // 2, 400)
    pet._check_edge()
    assert pet._hidden_edge is None
    assert pet.x() == normal[0] and pet.y() == normal[1]
    pet.hide()


def test_settings_pet_checkbox_persists(qapp, monkeypatch):
    """设置页「启用桌面宠物」复选框：默认关；勾选 → 持久化 pet_enabled=True + 回调触发。"""
    from PySide6.QtWidgets import QCheckBox
    store = {}
    monkeypatch.setattr("app.ui.panels.settings_panel.load_ui_prefs",
                        lambda: dict(store))
    monkeypatch.setattr("app.ui.panels.settings_panel.save_ui_prefs",
                        lambda patch: store.update(patch))
    from app.ui.panels.settings_panel import SettingsPanel
    calls = []
    p = SettingsPanel(pet_toggle_cb=calls.append)
    cb = p.cb_pet
    assert isinstance(cb, QCheckBox)
    assert cb.isChecked() is False, "默认关闭"
    cb.setChecked(True)
    assert store.get("pet_enabled") is True
    assert calls == [True]
    cb.setChecked(False)
    assert store.get("pet_enabled") is False
    assert calls == [True, False]


def test_main_tabs_pet_enabled_creates_and_toggle_hides(qapp, monkeypatch):
    """MainTabs：pet_enabled=True 时启动即创建并显示；回调关闭后隐藏。"""
    from app.ui.widgets import main_tabs as mt
    created = []

    class FakePet:
        def __init__(self, *a, **k):
            created.append(self)
            self.visible = False

        def set_visible_with_pet(self, on):
            self.visible = on

    monkeypatch.setattr("app.ui.widgets.pet_window.PetWindow", FakePet)
    monkeypatch.setattr(mt, "load_ui_prefs", lambda: {"pet_enabled": True})
    tabs = mt.MainTabs(automation_service=None, cooldown_manager=None, music=None)
    assert len(created) == 1
    assert created[0].visible is True, "启动即显示桌宠"
    tabs._on_pet_toggle(False)
    assert created[0].visible is False
