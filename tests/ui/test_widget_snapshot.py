"""Regression for GC re-entry while PySide converts native widget lists."""
import gc
import threading
import weakref

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from app.ui.theme.manager import get_theme_manager
from app.ui.widget_snapshot import snapshot_widgets

pytestmark = pytest.mark.ui


@pytest.fixture(autouse=True)
def restore_gc_state():
    enabled, thresholds = gc.isenabled(), gc.get_threshold()
    try:
        yield
    finally:
        gc.set_threshold(*thresholds)
        if enabled:
            gc.enable()
        else:
            gc.disable()


def test_theme_enumerates_widgets_with_automatic_gc_paused(tmp_path, monkeypatch):
    from app.ui import settings_store

    monkeypatch.setattr(settings_store, "UI_PREFS_PATH", str(tmp_path / "prefs.json"))
    app = QApplication.instance() or QApplication([])
    manager = get_theme_manager(app)
    dialog = QDialog()
    observed = []
    native = QApplication.allWidgets

    def enumerate_widgets():
        observed.append(gc.isenabled())
        return native()

    monkeypatch.setattr(QApplication, "allWidgets", staticmethod(enumerate_widgets))
    original = gc.isenabled()
    manager.set_theme(manager.mode, force=True, persist=False)
    assert observed == [False]
    assert gc.isenabled() == original
    assert dialog in snapshot_widgets(app)


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("top_level", [True, False])
def test_snapshot_materializes_the_entire_list_before_restoring_gc(monkeypatch, enabled, top_level):
    app = QApplication.instance() or QApplication([])
    dialog = QDialog()
    observed = []

    def enumerate_widgets():
        observed.append(gc.isenabled())

        def values():
            observed.append(gc.isenabled())
            yield dialog
            observed.append(gc.isenabled())

        return values()

    name = "topLevelWidgets" if top_level else "allWidgets"
    monkeypatch.setattr(QApplication, name, staticmethod(enumerate_widgets))
    (gc.enable if enabled else gc.disable)()
    assert snapshot_widgets(app, top_level=top_level) == [dialog]
    assert observed == [False, False, False]
    assert gc.isenabled() is enabled


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("during_iteration", [True, False])
def test_snapshot_restores_gc_when_enumeration_or_materialization_fails(monkeypatch, enabled, during_iteration):
    app = QApplication.instance() or QApplication([])

    def enumerate_widgets():
        assert not gc.isenabled()
        if not during_iteration:
            raise RuntimeError("enumeration failed")

        def values():
            assert not gc.isenabled()
            raise RuntimeError("conversion failed")
            yield  # Make failure occur during list materialization.

        return values()

    monkeypatch.setattr(QApplication, "allWidgets", staticmethod(enumerate_widgets))
    (gc.enable if enabled else gc.disable)()
    with pytest.raises(RuntimeError, match="failed"):
        snapshot_widgets(app)
    assert gc.isenabled() is enabled


def test_real_dialog_cycle_survives_automatic_gc_until_snapshot_is_released(monkeypatch):
    app = QApplication.instance() or QApplication([])
    native = QApplication.topLevelWidgets
    destroyed, weak = [], []

    def enumerate_widgets():
        assert not gc.isenabled()
        dialog = QDialog()
        dialog.setObjectName("snapshot-owned-cycle")
        dialog.destroyed.connect(lambda: destroyed.append(threading.get_ident()))
        dialog.cycle = dialog
        weak.append(weakref.ref(dialog))
        del dialog
        # Force allocation pressure with the lowest automatic GC threshold.
        pressure = [[i] for i in range(100)]
        assert pressure and not destroyed
        return native()

    gc.set_threshold(1, 1, 1)
    gc.enable()
    with monkeypatch.context() as patch:
        patch.setattr(QApplication, "topLevelWidgets", staticmethod(enumerate_widgets))
        snapshot = snapshot_widgets(app, top_level=True)
    assert gc.isenabled()
    assert any(w.objectName() == "snapshot-owned-cycle" for w in snapshot)
    gc.collect()
    assert weak[0]() is not None and not destroyed
    snapshot.clear()
    gc.collect()
    assert weak[0]() is None
    assert destroyed == [threading.get_ident()]


def test_nested_snapshots_restore_the_outer_gc_state(monkeypatch):
    app = QApplication.instance() or QApplication([])
    native = QApplication.topLevelWidgets
    observed = []

    def enumerate_widgets():
        assert not gc.isenabled()
        result = snapshot_widgets(app, top_level=True)
        observed.append(gc.isenabled())
        return result

    monkeypatch.setattr(QApplication, "allWidgets", staticmethod(enumerate_widgets))
    gc.enable()
    assert snapshot_widgets(app) == snapshot_widgets(app, top_level=True)
    assert observed == [False] and gc.isenabled()


def test_background_thread_is_rejected_before_gc_or_widget_enumeration(monkeypatch):
    app = QApplication.instance() or QApplication([])
    observed = []
    monkeypatch.setattr(QApplication, "allWidgets", staticmethod(lambda: observed.append("read")))
    original = gc.isenabled()

    def worker():
        try:
            snapshot_widgets(app)
        except RuntimeError as error:
            observed.append(str(error))

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join(1)
    assert not thread.is_alive()
    assert observed == ["Widget snapshots must be taken on the GUI thread"]
    assert gc.isenabled() is original
