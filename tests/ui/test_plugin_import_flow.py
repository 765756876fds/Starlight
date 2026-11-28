"""Configuration import and source presentation in the music settings UI."""
import json
import os
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytestmark = pytest.mark.ui


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    yield QApplication.instance() or QApplication([])


@pytest.mark.parametrize("suffix", [".js", ".json"])
def test_script_is_never_saved_as_an_active_source(qapp, tmp_path, monkeypatch, suffix):
    from app.ui.dialogs import source_manage_dialog as ui
    from app.music.source_manager import MusicSourceManager

    path = tmp_path / f"selected{suffix}"
    path.write_text("module.exports = {platform: 'test'};", encoding="utf-8")
    manager = MusicSourceManager(str(tmp_path))
    notified = Mock()
    warning, critical = Mock(), Mock()
    monkeypatch.setattr(ui, "glass_warning", warning)
    monkeypatch.setattr(ui, "glass_critical", critical)
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    dialog = ui.SourceManageDialog(manager=manager, on_sources_changed=notified)
    try:
        dialog.btn_import_file.click()
        assert list(tmp_path.iterdir()) == [path]
        assert manager.get_all_sources() == []
        notified.assert_not_called()
        if suffix == ".js":
            assert "受控入口" in warning.call_args.args[2]
        else:
            critical.assert_called_once()
    finally:
        dialog.close()


def test_json_import_toggle_duplicate_and_remove(qapp, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from app.ui.dialogs import source_manage_dialog as ui
    from app.music.source_manager import MusicSourceManager

    config = {"name": "我的目录", "api_url": "https://catalog.example.test/config?private=example"}
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    manager = MusicSourceManager(str(tmp_path))
    notified = Mock()
    info, critical = Mock(), Mock()
    monkeypatch.setattr(ui, "glass_info", info)
    monkeypatch.setattr(ui, "glass_critical", critical)
    monkeypatch.setattr(ui, "glass_question", lambda *_: True)
    monkeypatch.setattr(ui.QFileDialog, "getOpenFileName", lambda *_: (str(path), ""))
    dialog = ui.SourceManageDialog(manager=manager, on_sources_changed=notified)
    try:
        dialog.btn_import_file.click()
        item = dialog.list_widget.item(0)
        assert "已启用" in item.text()
        assert "catalog.example.test" in item.text()
        assert "private" not in item.text()
        dialog.btn_import_file.click()
        assert "同名" in critical.call_args.args[2]
        assert notified.call_count == 1
        item.setCheckState(Qt.Unchecked)
        assert "已停用" in item.text()
        assert MusicSourceManager(str(tmp_path)).get_enabled_sources() == []
        item.setCheckState(Qt.Checked)
        assert "已启用" in item.text()
        dialog.list_widget.setCurrentItem(item)
        dialog.btn_delete.click()
        assert MusicSourceManager(str(tmp_path)).get_all_sources() == []
        assert notified.call_count == 4
    finally:
        dialog.close()


def test_controlled_settings_reports_roles_without_legacy_actions(qapp):
    import time
    from types import SimpleNamespace
    from PySide6.QtWidgets import QLabel
    from app.ui.panels.settings_panel import SettingsPanel

    overview = Mock(return_value=[{
        "name": "lab", "version": "1.0.0", "kind": "musicfree",
        "state": "等待审查或授权", "providers": (), "services": (),
        "routes": (("kw", ("resolver",)),),
    }])
    online = SimpleNamespace(allow_legacy_sources=False, isolated_source_overview=overview)
    panel = SettingsPanel(music=SimpleNamespace(online_manager=online))
    try:
        assert panel.btn_source_manage.text() == "查看受控音源"
        assert not hasattr(panel, "btn_check_updates")
        until = time.monotonic() + 3
        while not panel.btn_refresh_plugins.isEnabled() and time.monotonic() < until:
            qapp.processEvents()
            time.sleep(0.01)
        overview.assert_called_once_with()
        status = panel._plugins_container.findChild(QLabel).text()
        assert "MusicFree 搜索目录" in status
        assert "等待审查或授权" in status
        assert "kw → resolver" in status
    finally:
        panel.close()


class _FakeMusic:
    def __init__(self, sources):
        self._sources = list(sources)

    def get_online_sources(self):
        return list(self._sources)

    def get_play_mode(self):
        return "sequential"

    def is_highlight_mode(self):
        return False

    def get_volume(self):
        return 70

    def set_volume(self, v):
        pass

    def search_local(self, kw=""):
        return []

    def get_current_song(self):
        return None


def test_music_panel_refresh_online_sources(qapp):
    """刷新后搜索下拉获得最新源列表；选中项保留，失效回退「全部」。"""
    from app.ui.panels.music_panel import MusicPanel

    fake = _FakeMusic(["js_bridge", "old_src"])
    panel = MusicPanel(music=fake)
    try:
        names = [panel.cb_source.itemData(i) for i in range(panel.cb_source.count())]
        assert names == [None, "js_bridge", "old_src"]

        # 选中 old_src → 外部源列表变化（old_src 失效，新增 new_src）
        idx = panel.cb_source.findData("old_src")
        panel.cb_source.setCurrentIndex(idx)
        fake._sources = ["js_bridge", "new_src"]
        panel.refresh_online_sources()

        names2 = [panel.cb_source.itemData(i) for i in range(panel.cb_source.count())]
        assert names2 == [None, "js_bridge", "new_src"], "下拉应更新为最新列表"
        assert panel.cb_source.currentData() is None, "失效选中项应回退「全部」"

        # 选中有效项 → 刷新后保留
        i2 = panel.cb_source.findData("js_bridge")
        panel.cb_source.setCurrentIndex(i2)
        panel.refresh_online_sources()
        assert panel.cb_source.currentData() == "js_bridge", "有效选中项应保留"
    finally:
        panel._timer.stop()


def test_music_panel_refresh_no_music_no_crash(qapp):
    from app.ui.panels.music_panel import MusicPanel
    panel = MusicPanel(music=None)
    try:
        panel.refresh_online_sources()   # 不应抛异常
    finally:
        panel._timer.stop()
