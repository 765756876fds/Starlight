"""Small on-demand diagnostics view. Does not initialize business components."""
import sys

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget

from ...core.self_check import collect_report
from ...core.build_info import describe_build


class _CheckThread(QThread):
    result = Signal(object)

    def run(self):
        try:
            report = collect_report()
        except Exception as exc:
            report = {"ok": False, "checks": [{"component": "environment_check",
                "ok": False, "error_type": type(exc).__name__}]}
        self.result.emit(report)


class DiagnosticsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._worker = None
        layout = QVBoxLayout(self)
        mode = "安装版" if getattr(sys, "frozen", False) else "源码开发版"
        layout.addWidget(QLabel(f"当前运行：{mode} · Python {sys.version.split()[0]}"))
        self.build_label = QLabel(describe_build())
        self.build_label.setWordWrap(True)
        layout.addWidget(self.build_label)
        layout.addWidget(QLabel("检查依赖是否可加载；不会录音、联网识曲或启动播放。"))
        self.run_button = QPushButton("检查运行环境")
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.run_button)
        self.output = QTextEdit()
        self.output.setReadOnly(True)
        self.output.setPlainText("尚未检查。遇到识曲或启动问题时，可运行检查并复制结果。")
        layout.addWidget(self.output)
        copy_button = QPushButton("复制检查结果")
        copy_button.clicked.connect(lambda: QApplication.clipboard().setText(
            self.build_label.text() + "\n" + self.output.toPlainText()))
        layout.addWidget(copy_button)

    def _start(self):
        if self._worker is not None:
            return
        self.run_button.setEnabled(False)
        self.output.setPlainText("正在检查…")
        self._worker = _CheckThread(self)
        self._worker.result.connect(self._show_result)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _show_result(self, report):
        lines = ["运行环境检查通过" if report["ok"] else "部分依赖无法加载"]
        labels = {"PySide6.QtCore": "界面基础组件", "PySide6.QtWidgets": "界面控件",
                  "pydantic": "在线识曲数据校验", "pydantic_core": "在线识曲原生组件",
                  "shazamio_core": "在线识曲音频分析", "aiohttp": "在线请求组件",
                  "shazamio": "在线识曲服务接口", "environment_check": "环境检查"}
        for check in report["checks"]:
            detail = "可加载" if check["ok"] else check["error_type"]
            if check.get("missing_module"):
                detail += f"：缺少 {check['missing_module']}"
            lines.append(f"{labels.get(check['component'], check['component'])}：{detail}")
        if not report["ok"]:
            lines.append("建议：复制本页检查结果提供给维护者；无需修改音乐库或重新建立指纹库。")
        lines.append("此结果不代表在线服务、音频设备或游戏功能已验收。")
        self.output.setPlainText("\n".join(lines))

    def _finished(self):
        worker = self._worker
        self._worker = None
        self.run_button.setEnabled(True)
        if worker is not None:
            worker.deleteLater()
