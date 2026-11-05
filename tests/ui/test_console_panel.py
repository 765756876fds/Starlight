"""The former console is now a notification view; raw diagnostic logs stay on disk."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from app.core.notifications import NotificationCenter
from app.ui.panels.console_panel import ConsolePanel


def test_history_limit_keeps_latest_200_notifications():
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    for index in range(300):
        center.post(f"操作 {index}", status="success")
    panel = ConsolePanel(center=center)
    assert panel.model.rowCount() == 200
    assert panel.model.index(0).data(Qt.UserRole).title == "操作 299"
    assert panel.model.index(199).data(Qt.UserRole).title == "操作 100"
    panel.stream.dispose()
    panel.close()


def test_progress_updates_one_row_and_selected_details():
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    panel = ConsolePanel(center=center)
    key = center.post("正在搜索歌曲")
    QTest.qWait(10)
    center.update(key, title="已找到 12 首歌曲", status="success", phase="done")
    QTest.qWait(10)
    assert panel.model.rowCount() == 1
    assert "已找到 12 首歌曲" in panel.details.toPlainText()
    assert "正在搜索歌曲" not in panel.details.toPlainText()
    panel.btn_clear.click()
    QTest.qWait(10)
    assert panel.model.rowCount() == 0
    assert panel.details.toPlainText() == ""
    panel.stream.dispose()
    panel.close()


def test_technical_diagnostics_remain_on_disk_without_ui_projection(tmp_path):
    from app.core.log_redirect import TeeStream
    app = QApplication.instance() or QApplication([])
    panel = ConsolePanel(center=NotificationCenter())
    stream = TeeStream(log_dir=str(tmp_path))
    raw = "Intent(music.pause, conf=1, params={}) | CommandBus handler | Traceback IPC"
    stream.write(raw + "\n")
    stream.flush()
    QTest.qWait(10)
    assert panel.model.rowCount() == 0
    assert raw in next(tmp_path.glob("print-*.log")).read_text(encoding="utf-8")
    if stream._file:
        stream._file.close()
    panel.stream.dispose()
    panel.close()


def test_notification_details_are_plain_text():
    app = QApplication.instance() or QApplication([])
    center = NotificationCenter()
    center.post("音乐已暂停", heard="<b>暂停音乐</b>", status="success")
    panel = ConsolePanel(center=center)
    assert "<b>暂停音乐</b>" in panel.details.toPlainText()
    panel.stream.dispose()
    panel.close()
