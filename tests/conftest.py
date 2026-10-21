# -*- coding: utf-8 -*-
"""pytest 配置和共享 fixtures

测试隔离网（防污染）：
- QT_QPA_PLATFORM=offscreen 在模块顶层强注入，早于任何 PySide6 import；
- 用例级 autouse teardown：驱动 Qt 事件循环清空残留信号/定时器，
  并 close 所有可见的顶层 QWidget（防上一用例的窗口/定时器漏到下一用例）；
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
    """用例级 Qt 隔离 teardown：驱散残留事件后关闭孤儿顶层窗口。
    无 QApplication 的纯逻辑用例自动跳过。"""
    yield
    try:
        from PySide6.QtCore import QCoreApplication
        app = QCoreApplication.instance()
        if app is None:
            return
        # 1) 清空事件队列：残留的 signal/singleShot/timer 回调先被驱动完
        for _ in range(3):
            app.processEvents()
        # 2) 关闭所有仍可见的顶层 QWidget（qtbot 管理的窗口已由其 teardown 处理，
        #    这里兜底清理测试自行创建的孤儿窗口，防定时器/重绘跨用例泄漏）
        from PySide6.QtWidgets import QApplication
        for w in app.topLevelWidgets():
            try:
                if w.isVisible():
                    w.close()
            except Exception:
                pass
        # 3) 二次驱散：close 触发的销毁事件也清空
        for _ in range(3):
            app.processEvents()
    except Exception:
        pass
