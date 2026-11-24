# -*- coding: utf-8 -*-
"""pytest 配置和共享 fixtures

测试隔离网（防污染）：
- QT_QPA_PLATFORM=offscreen 在模块顶层强注入，早于任何 PySide6 import；
- 用例级 autouse teardown：驱动 Qt 事件循环清空残留信号/定时器，
    并在 GUI 线程销毁本用例新增窗口（含隐藏窗口），保留外层 fixture 的窗口；
- EventBus：测试用全新实例（event_bus fixture）；全局单例不在用例内改写。
"""
import os
import sys

# 固定无头 Qt 平台：必须在任何 PySide6 import 之前（历史全量测试卡死根因之一）
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 将项目根目录加入 Python 路径
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


@pytest.fixture
def event_bus():
    """全新的 EventBus 实例"""
    from app.core.event_bus import EventBus
    return EventBus()


@pytest.fixture
def test_db_path(tmp_path):
    """临时数据库路径"""
    return str(tmp_path / "test_music.db")


@pytest.fixture
def music_db(test_db_path):
    """临时音乐数据库"""
    from app.music.database import MusicDatabase
    db = MusicDatabase(test_db_path)
    yield db
    db.close()


@pytest.fixture(autouse=True)
def _qt_process_cleanup():
    """Own newly created windows; never destroy a live worker's parent."""
    from PySide6.QtCore import QCoreApplication, QEvent, QThread
    from PySide6.QtWidgets import QApplication
    from shiboken6 import isValid
    from app.ui.widget_snapshot import snapshot_widgets
    app = QCoreApplication.instance()
    existing = set(snapshot_widgets(app, top_level=True)) if isinstance(app, QApplication) else set()
    yield
    app = QCoreApplication.instance()
    if not isinstance(app, QApplication):
        return
    for _ in range(3):
        app.processEvents()
    owned = [w for w in snapshot_widgets(app, top_level=True) if w not in existing]
    for window in owned:
        if not isValid(window):
            continue
        threads = list(window.findChildren(QThread))
        for name in ("_identify_thread", "_online_thread", "_build_thread"):
            thread = getattr(window, name, None)
            if isinstance(thread, QThread) and isValid(thread):
                threads.append(thread)
        assert not any(t.isRunning() for t in threads), "test window still owns a running QThread"
        window.close()
        # close() can merely hide/ignore, deliberately, in the real product.
        # The test owns these objects and must also release their Qt resources.
        if isValid(window):
            window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    for _ in range(3):
        app.processEvents()


@pytest.fixture(autouse=True)
def _disable_trace_in_tests(monkeypatch):
    """测试隔离：强制关闭影子对拍 trace（不读本地 config/settings.yaml）。

    本地配置可能已开启 music.trace_enabled（实机采集默认开），测试必须独立于该
    配置：否则 RecordingMpv 包装会破坏 `ctrl.mpv is fake` 注入断言，且测试进程
    会向 logs/trace 写真实 trace 文件。生产行为不受影响（默认仍读 settings.yaml）。"""
    from app.music.controller import MusicController

    monkeypatch.setattr(MusicController, "_load_trace_enabled", lambda self: False)
    yield
