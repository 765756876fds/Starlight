"""批 3b 桌面歌词自定义测试：设置持久化 + 设置页 UI + 歌词窗渲染/位置适配。

覆盖（防漏测重点）：
- settings_store：music.lyrics 读写（monkeypatch SETTINGS_PATH → tmp，不污染真实配置）
- LyricsWindow.apply_prefs：字体/字号/颜色/透明度/单双行即时生效 + 缓存清理 + 窗口高度
- 位置记忆：拖拽松手保存坐标；恢复/越界回中央（fake screens，不依赖 offscreen 几何）
- 设置页 UI：控件存在 + 初始值从 prefs 读出 + 监听器即时生效
"""
import os
import sys
from unittest.mock import MagicMock

import pytest
pytestmark = pytest.mark.ui

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app.ui.settings_store as settings_store  # noqa: E402
from app.ui.panels.settings_panel import (  # noqa: E402
    SettingsPanel, register_lyrics_prefs_listener,
)
from app.ui.widgets.lyrics_window import LyricsWindow  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    a = QApplication.instance() or QApplication([])
    return a


@pytest.fixture
def yaml_path(tmp_path, monkeypatch):
    """把 settings_store 的写盘目标重定向到 tmp（真实 settings.yaml 永不污染）。"""
    p = tmp_path / "settings.yaml"
    p.write_text("app:\n  name: t\nmusic:\n  scan_dirs: []\n", encoding="utf-8")
    monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(p))
    return p


def _controller(current_song=None, online_manager=None):
    ctrl = MagicMock()
    ctrl.event_bus = MagicMock()
    ctrl.mpv = MagicMock()
    ctrl.get_current_song.return_value = current_song
    ctrl.online_manager = online_manager
    return ctrl


# ---------- 持久化：settings.yaml music.lyrics ----------

class TestLyricsPrefsStore:
    def test_load_defaults_when_missing(self, yaml_path):
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["font"] == "Microsoft YaHei"
        assert prefs["font_size"] == 24
        assert prefs["color"] == "#FAB446"
        assert prefs["bg_opacity"] == 0
        assert prefs["mode"] == "triple"
        assert prefs["pos_x"] is None and prefs["pos_y"] is None

    def test_save_then_load_roundtrip(self, yaml_path):
        ok = settings_store.save_lyrics_prefs({
            "font": "SimHei", "font_size": 36, "color": "#FF0000",
            "bg_opacity": 50, "mode": "single", "pos_x": 100, "pos_y": 200,
        })
        assert ok
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["font"] == "SimHei"
        assert prefs["font_size"] == 36
        assert prefs["color"] == "#FF0000"
        assert prefs["bg_opacity"] == 50
        assert prefs["mode"] == "single"
        assert prefs["pos_x"] == 100 and prefs["pos_y"] == 200

    def test_save_ignores_unknown_keys(self, yaml_path):
        ok = settings_store.save_lyrics_prefs({"not_a_key": 1, "font_size": 20})
        assert ok
        prefs = settings_store.load_lyrics_prefs()
        assert "not_a_key" not in prefs
        assert prefs["font_size"] == 20

    def test_save_reset_position_none(self, yaml_path):
        settings_store.save_lyrics_prefs({"pos_x": 100, "pos_y": 200})
        settings_store.save_lyrics_prefs({"pos_x": None, "pos_y": None})
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["pos_x"] is None and prefs["pos_y"] is None

    def test_save_keeps_existing_pos(self, yaml_path):
        """防"点锁定跑回中央"根因 A：设置页普通改动（collect 无 pos 键）写盘时，
        绝不能抹掉 yaml 里已保存的位置（旧实现浅合并 → lyrics 整段替换 → pos 丢失）。"""
        settings_store.save_lyrics_prefs({"pos_x": 1079, "pos_y": 21})
        settings_store.save_lyrics_prefs({"locked": False})   # 设置页锁定改动形态
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["pos_x"] == 1079 and prefs["pos_y"] == 21   # 位置保留
        assert prefs["locked"] is False
        # 连续多次普通改动也不丢
        settings_store.save_lyrics_prefs({"font_size": 30})
        assert settings_store.load_lyrics_prefs()["pos_x"] == 1079

    def test_corrupted_yaml_falls_back_to_defaults(self, tmp_path, monkeypatch):
        p = tmp_path / "settings.yaml"
        p.write_text("{{{{ 非法 yaml", encoding="utf-8")
        monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(p))
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["font_size"] == 24  # 回退默认，不崩


# ---------- 歌词窗：apply_prefs 即时生效 ----------

class TestLyricsWindowPrefs:
    @pytest.fixture(autouse=True)
    def _isolate_settings(self, tmp_path, monkeypatch):
        """类内所有用例把 settings_store 写盘目标重定向到 tmp：
        不依赖真实 settings.yaml（用户可能已自定义颜色等 → 默认值断言被破坏）。
        """
        p = tmp_path / "settings.yaml"
        p.write_text("app:\n  name: t\nmusic:\n  scan_dirs: []\n", encoding="utf-8")
        monkeypatch.setattr(settings_store, "SETTINGS_PATH", str(p))
        yield

    def test_default_construction(self, qapp):
        w = LyricsWindow(_controller())
        assert w._prefs["font"] == "Microsoft YaHei"
        assert w._prefs["font_size"] == 24
        assert w._prefs["mode"] == "triple"
        assert w._prefs["locked"] is True
        assert w._color_cur == QColor("#FAB446")
        assert w._fm is not None
        # 3 行模式高度 = 3 * 实际字体行高 + 上下边距
        assert w._line_h == w._fm.height() + 8
        assert w.height() == 3 * w._line_h + 12

    def test_apply_font_and_size(self, qapp):
        w = LyricsWindow(_controller())
        w.set_lyrics([(1.0, "行一"), (2.0, "行二")], "歌")
        cache_was = dict(w._row_cache)  # 触发一次缓存
        w._row_pixmap("行一", True)
        assert len(w._row_cache) == 1
        # 先固定字体（不同字体的 QFontMetrics 行高不可比——SimHei 30 可能低于雅黑 24）
        w.apply_prefs({"font": "SimHei", "font_size": 24})
        assert w._prefs["font"] == "SimHei"
        assert w._prefs["font_size"] == 24
        assert w._font.family() == "SimHei"
        assert w._font.pointSize() == 24
        assert w._row_cache == {}            # 换字体 → 清缓存重算
        assert w._line_h == w._fm.height() + 8   # 行高 = 实际字体行高 + 边距
        old_h = w.height()
        w.apply_prefs({"font": "SimHei", "font_size": 30})
        assert w._prefs["font_size"] == 30
        assert w._font.pointSize() == 30
        assert w._row_cache == {}
        assert w._line_h == w._fm.height() + 8
        assert w.height() > old_h            # 同字体字号变大 → 窗口变高

    def test_apply_color(self, qapp):
        w = LyricsWindow(_controller())
        w.set_lyrics([(1.0, "行")], "歌")
        w._row_pixmap("行", True)
        assert w._row_cache  # 有缓存
        w.apply_prefs({"color": "#00FF00"})
        assert w._color_cur == QColor("#00FF00")
        assert w._row_cache == {}            # 颜色变 → 缓存失效
        pm = w._row_pixmap("行", True)
        assert pm is not None

    def test_apply_mode_single_shrinks(self, qapp):
        w = LyricsWindow(_controller())
        h3 = w.height()
        w.apply_prefs({"mode": "single"})
        assert w._prefs["mode"] == "single"
        assert w._line_count() == 1
        assert w.height() < h3              # 单行 → 窗口矮
        w.apply_prefs({"mode": "triple"})
        assert w.height() == h3

    def test_apply_bg_opacity(self, qapp):
        w = LyricsWindow(_controller())
        w.apply_prefs({"bg_opacity": 40})
        assert w._prefs["bg_opacity"] == 40
        # 渲染不崩（含背景分支）
        from PySide6.QtGui import QPaintEvent
        w.set_lyrics([(1.0, "行")], "歌")
        w.paintEvent(QPaintEvent(w.rect()))

    def test_apply_noop_same_prefs(self, qapp):
        w = LyricsWindow(_controller())
        prefs = dict(w._prefs)
        w.apply_prefs(prefs)
        assert w._prefs == prefs  # 幂等：无变化

    def test_apply_settings_does_not_move_window(self, qapp):
        """批 3b 体验修复：改字体/锁定/颜色等（无 pos 键）→ 绝不挪动/绝不 recenter。"""
        w = LyricsWindow(_controller())
        w.move(300, 200)
        before = (w.pos().x(), w.pos().y())
        w.apply_prefs({"locked": False, "font_size": 30, "color": "#00FF00"})
        assert (w.pos().x(), w.pos().y()) == before   # 位置原封不动
        w.apply_prefs({"mode": "single"})
        assert (w.pos().x(), w.pos().y()) == before   # 模式切换也不动

    def test_apply_prefs_with_pos_moves(self, qapp, monkeypatch):
        """显式带 pos 的 prefs（拖拽保存/重置意图）→ 才处理位置。"""
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.screens",
            lambda: [_FakeScreen(QRect(0, 0, 1920, 1080))],
        )
        w = LyricsWindow(_controller())
        w.apply_prefs({"pos_x": 700, "pos_y": 300})
        assert w.pos().x() == 700 and w.pos().y() == 300

    def test_paint_triple_and_single(self, qapp):
        """3 行/单行渲染都不崩（防"只在真实渲染才执行"漏测）。"""
        from PySide6.QtGui import QPaintEvent
        w = LyricsWindow(_controller())
        w.set_lyrics([(1.0, "一"), (2.0, "二"), (3.0, "三"), (4.0, "四")], "歌")
        w._current_idx = 2
        w.paintEvent(QPaintEvent(w.rect()))
        w.apply_prefs({"mode": "single"})
        w.paintEvent(QPaintEvent(w.rect()))


# ---------- 窗高计算：文字不被裁剪（空/单行/多行/大字号） ----------

class TestLyricsWindowHeight:
    def test_height_single_and_triple(self, qapp):
        """单行=1 行高+边距；3 行=3 行高+边距。"""
        w = LyricsWindow(_controller())
        h3 = w.height()
        assert h3 == 3 * w._line_h + 12
        w.apply_prefs({"mode": "single"})
        assert w.height() == w._line_h + 12
        assert w._line_count() == 1
        w.apply_prefs({"mode": "triple"})
        assert w.height() == h3

    def test_large_font_text_fits_pixmap(self, qapp):
        """大字号：行 pixmap 高度 ≥ 实际字体高度（ascent+descent），文字不贴边被切。"""
        w = LyricsWindow(_controller())
        w.apply_prefs({"font_size": 48})
        ascent = w._fm.ascent()
        descent = w._fm.descent()
        assert w._line_h >= ascent + descent + 2   # 文字完整落在行内（+上下余量）
        pm = w._row_pixmap("测试歌词", True)
        assert pm.height() == w._line_h

    def test_empty_and_one_line_paint(self, qapp):
        """空歌词/单行歌词渲染不崩（防渲染路径回归）。"""
        from PySide6.QtGui import QPaintEvent
        w = LyricsWindow(_controller())
        w.paintEvent(QPaintEvent(w.rect()))          # 空 → 占位
        w.set_lyrics([(1.0, "只有一行")], "歌")
        w.paintEvent(QPaintEvent(w.rect()))          # 单行 → 当前行居中

    def test_placeholder_fits_height(self, qapp):
        """占位符也不超出窗口高度（大字号占位居中完整）。"""
        w = LyricsWindow(_controller())
        w.apply_prefs({"font_size": 40})
        pm = w._row_pixmap("♪ 暂无歌词", False)
        assert pm.height() <= w.height()


# ---------- 锁定/解锁开关 ----------

class TestLyricsWindowLock:
    def test_default_locked_passthrough(self, qapp, monkeypatch):
        """默认锁定 → 构造即穿透（_set_passthrough(True)）。"""
        calls = []
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.LyricsWindow._set_passthrough",
            lambda self, on: calls.append(on),
        )
        w = LyricsWindow(_controller())
        assert w._prefs["locked"] is True
        assert calls and calls[-1] is True

    def test_apply_prefs_unlock_flips_passthrough(self, qapp, monkeypatch):
        """设置页解锁 → 穿透关闭（可点中/可拖）。"""
        calls = []
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.LyricsWindow._set_passthrough",
            lambda self, on: calls.append(on),
        )
        w = LyricsWindow(_controller())
        w.apply_prefs({"locked": False})
        assert w._prefs["locked"] is False
        assert calls[-1] is False
        w.apply_prefs({"locked": True})
        assert calls[-1] is True

    def test_unlocked_can_drag(self, qapp, monkeypatch):
        """解锁态：左键按下 → 进入拖拽。"""
        w = LyricsWindow(_controller())
        w._prefs["locked"] = False
        ev = MagicMock()
        ev.button.return_value = Qt.LeftButton
        ev.globalPosition.return_value = MagicMock()
        w.mousePressEvent(ev)
        assert w._dragging is True

    def test_locked_ignores_drag(self, qapp):
        """锁定态：左键按下 → 不进入拖拽（穿透兜底 + guard 双保险）。"""
        w = LyricsWindow(_controller())
        w._prefs["locked"] = True
        ev = MagicMock()
        ev.button.return_value = Qt.LeftButton
        w.mousePressEvent(ev)
        assert w._dragging is False

    def test_unlock_release_keeps_unlocked(self, qapp, monkeypatch):
        """松手后保持解锁（不自动锁回），位置照常保存。"""
        seen = {}
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.save_lyrics_prefs",
            lambda update: seen.update(update) or True,
        )
        w = LyricsWindow(_controller())
        w._prefs["locked"] = False
        w.move(50, 60)
        ev = MagicMock()
        ev.button.return_value = Qt.LeftButton
        w.mousePressEvent(ev)
        w.mouseReleaseEvent(ev)
        assert w._prefs["locked"] is False    # 解锁保持
        assert seen.get("pos_x") == 50 and seen.get("pos_y") == 60

    def test_show_event_reapplies_passthrough(self, qapp, monkeypatch):
        """show 时补一次穿透（winId 在 show 后才有效）。"""
        calls = []
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.LyricsWindow._set_passthrough",
            lambda self, on: calls.append(on),
        )
        w = LyricsWindow(_controller())
        calls.clear()
        from PySide6.QtGui import QShowEvent
        w.showEvent(QShowEvent())
        assert calls and calls[-1] is True


# ---------- 位置记忆：保存 / 恢复 / 越界回中央 ----------

class _FakeScreen:
    def __init__(self, geo: QRect):
        self._geo = geo

    def availableGeometry(self):
        return self._geo


class TestLyricsWindowPosition:
    def test_pos_on_screen(self, qapp, monkeypatch):
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.screens",
            lambda: [_FakeScreen(QRect(0, 0, 1920, 1080))],
        )
        assert LyricsWindow._pos_on_screen(10, 10, 640, 108) is True
        assert LyricsWindow._pos_on_screen(99999, 99999, 640, 108) is False
        # 完全在屏外（x=-5000 到 -4360，与 0..1920 无交集）→ False
        assert LyricsWindow._pos_on_screen(-5000, 100, 640, 108) is False

    def test_restore_valid_pos(self, qapp, monkeypatch):
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.screens",
            lambda: [_FakeScreen(QRect(0, 0, 1920, 1080))],
        )
        w = LyricsWindow(_controller())
        w._apply_position({"pos_x": 120, "pos_y": 80})
        assert w.pos().x() == 120 and w.pos().y() == 80

    def test_out_of_bounds_recenters(self, qapp, monkeypatch):
        fake = _FakeScreen(QRect(0, 0, 1920, 1080))
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.screens",
            lambda: [fake],
        )
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.primaryScreen",
            lambda: fake,
        )
        w = LyricsWindow(_controller())
        w._apply_position({"pos_x": 99999, "pos_y": 99999})
        geo = fake.availableGeometry()
        assert w.pos().x() == geo.center().x() - w.width() // 2
        assert w.pos().y() == geo.center().y() - w.height() // 2

    def test_none_position_recenters(self, qapp, monkeypatch):
        fake = _FakeScreen(QRect(0, 0, 1920, 1080))
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.primaryScreen",
            lambda: fake,
        )
        w = LyricsWindow(_controller())
        w._apply_position({"pos_x": None, "pos_y": None})
        geo = fake.availableGeometry()
        assert w.pos().x() == geo.center().x() - w.width() // 2

    def test_drag_release_saves_position(self, qapp, monkeypatch):
        """拖拽松手 → 实时保存坐标（mouseReleaseEvent 钩子）。"""
        seen = {}
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.save_lyrics_prefs",
            lambda update: seen.update(update) or True,
        )
        w = LyricsWindow(_controller())
        w.move(300, 150)
        w._dragging = True
        w._drag_offset = MagicMock()
        ev = MagicMock()
        ev.button.return_value = Qt.LeftButton
        w.mouseReleaseEvent(ev)
        assert w._dragging is False
        assert seen.get("pos_x") == 300 and seen.get("pos_y") == 150

    def test_drag_release_non_left_button_ignored(self, qapp, monkeypatch):
        called = []
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.save_lyrics_prefs",
            lambda update: called.append(update) or True,
        )
        w = LyricsWindow(_controller())
        w._dragging = True
        ev = MagicMock()
        ev.button.return_value = Qt.RightButton
        w.mouseReleaseEvent(ev)
        assert called == []  # 非左键不保存


# ---------- 设置页 UI ----------

def _make_panel(qapp):
    music = MagicMock()
    music.online_manager = None
    return SettingsPanel(
        music=music, tts=MagicMock(), combo_manager=MagicMock(),
        equip_toggle=lambda on: None, on_refresh_changed=lambda ms: None,
        overlay_toggle=lambda on: None, overlay_enabled=True, parent=None,
    )


class TestSettingsPanelLyricsGroup:
    def test_controls_exist_with_defaults(self, qapp, yaml_path):
        p = _make_panel(qapp)
        assert p.cmb_lyrics_font is not None
        assert p.spin_lyrics_size is not None
        assert p.btn_lyrics_color is not None
        assert p.slider_lyrics_opacity is not None
        assert p.cmb_lyrics_mode is not None
        # 初始值从默认 prefs 读出（yaml 无 lyrics 段 → 默认）
        assert p.spin_lyrics_size.value() == 24
        assert p.slider_lyrics_opacity.value() == 0
        assert p.cmb_lyrics_mode.currentData() == "triple"

    def test_collect_prefs_from_controls(self, qapp, yaml_path):
        p = _make_panel(qapp)
        p.spin_lyrics_size.setValue(32)
        p.slider_lyrics_opacity.setValue(60)
        p.cmb_lyrics_mode.setCurrentIndex(p.cmb_lyrics_mode.findData("single"))
        prefs = p._collect_lyrics_prefs()
        assert prefs["font_size"] == 32
        assert prefs["bg_opacity"] == 60
        assert prefs["mode"] == "single"
        assert prefs["color"] == "#FAB446"

    def test_change_notifies_listeners(self, qapp, yaml_path, monkeypatch):
        """改动即时生效：保存 + 通知已开歌词窗（监听器收到合并后 prefs）。"""
        p = _make_panel(qapp)
        seen = []
        register_lyrics_prefs_listener(lambda prefs: seen.append(prefs))
        # 防止污染真实 yaml：save_lyrics_prefs 已被 yaml_path 重定向 → 走真实但写 tmp
        p.spin_lyrics_size.setValue(28)
        assert seen, "监听器应被通知"
        last = seen[-1]
        assert last["font_size"] == 28   # 窗口将收到的最新配置

    def test_opacity_label_updates(self, qapp, yaml_path):
        p = _make_panel(qapp)
        p.slider_lyrics_opacity.setValue(45)
        assert p.lbl_lyrics_opacity.text() == "45%"

    def test_color_button_click_no_dialog_crash(self, qapp, yaml_path, monkeypatch):
        """QColorDialog 在测试环境不可交互：mock getColor 返回合法色 → 保存生效。"""
        p = _make_panel(qapp)
        monkeypatch.setattr(
            "app.ui.panels.settings_panel.QColorDialog.getColor",
            lambda *a, **k: QColor("#112233"),
        )
        p._on_lyrics_color()
        assert p._lyrics_color == QColor("#112233")
        prefs = p._collect_lyrics_prefs()
        assert prefs["color"] == "#112233"

    def test_reset_position_button(self, qapp, yaml_path):
        p = _make_panel(qapp)
        p._on_lyrics_reset_pos()
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["pos_x"] is None and prefs["pos_y"] is None

    def test_locked_checkbox_exists_default_checked(self, qapp, yaml_path):
        p = _make_panel(qapp)
        assert p.cb_lyrics_locked is not None
        assert p.cb_lyrics_locked.isChecked() is True   # 默认锁定
        assert p._collect_lyrics_prefs()["locked"] is True

    def test_locked_toggle_notifies(self, qapp, yaml_path):
        """解锁勾选 → 保存 + 通知歌词窗（监听器收到 locked=False）。"""
        p = _make_panel(qapp)
        seen = []
        register_lyrics_prefs_listener(lambda prefs: seen.append(prefs))
        p.cb_lyrics_locked.setChecked(False)
        assert seen and seen[-1]["locked"] is False
        assert settings_store.load_lyrics_prefs()["locked"] is False
        p.cb_lyrics_locked.setChecked(True)
        assert seen[-1]["locked"] is True

    def test_construction_does_not_save(self, qapp, yaml_path):
        """防再犯（QFontComboBox.setCurrentFont 构造期触发 *Changed → 误写盘）：
        构造 SettingsPanel 后 yaml 里不得出现 lyrics 段（构造期信号全忽略）。"""
        p = _make_panel(qapp)
        assert p._lyrics_group_ready is True
        prefs = settings_store.load_lyrics_prefs()
        assert prefs["font_size"] == 24          # 默认值——未被构造期信号改掉
        assert prefs["mode"] == "triple"
        assert prefs["locked"] is True


# ---------- 防"点锁定瞬间跑回中央"（位置门控全链） ----------

class TestLockDoesNotMoveWindow:
    def test_plain_change_notify_strips_pos(self, qapp, yaml_path):
        """根因 B：设置页普通改动（update 无 pos 键）→ 通知 prefs 剥离 pos——
        窗口 apply_prefs 的"仅显式含 pos 才移动"门控不触发。"""
        from app.ui.panels.settings_panel import _notify_lyrics_prefs_changed
        settings_store.save_lyrics_prefs({"pos_x": 1079, "pos_y": 21})
        seen = []
        register_lyrics_prefs_listener(lambda prefs: seen.append(prefs))
        _notify_lyrics_prefs_changed({"locked": False})          # 设置页锁定改动形态
        assert seen and "pos_x" not in seen[-1] and "pos_y" not in seen[-1]

    def test_reset_notify_keeps_pos_key(self, qapp, yaml_path):
        """重置位置（update 显式含 pos=None）→ 通知保留 pos 键 → 窗口回中央。"""
        from app.ui.panels.settings_panel import _notify_lyrics_prefs_changed
        seen = []
        register_lyrics_prefs_listener(lambda prefs: seen.append(prefs))
        _notify_lyrics_prefs_changed({"pos_x": None, "pos_y": None})
        assert seen and seen[-1]["pos_x"] is None and seen[-1]["pos_y"] is None

    def test_lock_via_panel_does_not_move_window(self, qapp, yaml_path, monkeypatch):
        """全链复现用户场景：拖到 (1079,21) → 点锁定（设置页 collect 形态）→
        窗口位置必须保持 (1079,21)，绝不跑回中央。"""
        from app.ui.panels.settings_panel import register_lyrics_prefs_listener as reg
        from PySide6.QtCore import QRect as _QR
        monkeypatch.setattr(
            "app.ui.widgets.lyrics_window.QGuiApplication.screens",
            lambda: [MagicMock(availableGeometry=lambda: _QR(0, 0, 1920, 1080))],
        )
        # 1. 拖拽保存位置（窗口构造读 yaml pos → 恢复）
        settings_store.save_lyrics_prefs({"pos_x": 1079, "pos_y": 21})
        w = LyricsWindow(_controller())
        assert (w.pos().x(), w.pos().y()) == (1079, 21)
        # 2. 点锁定：设置页 collect（无 pos 键）→ 保存 + 通知 → 窗口 apply_prefs
        p = _make_panel(qapp)
        reg(w.apply_prefs)
        p._apply_lyrics_prefs(p._collect_lyrics_prefs())   # locked=True 勾选态保存
        # 3. 断言：位置原封不动（不是屏幕中央）
        assert (w.pos().x(), w.pos().y()) == (1079, 21)
        # 4. 解锁再锁（双方向）也不动
        p.cb_lyrics_locked.setChecked(False)
        assert (w.pos().x(), w.pos().y()) == (1079, 21)
        p.cb_lyrics_locked.setChecked(True)
        assert (w.pos().x(), w.pos().y()) == (1079, 21)
