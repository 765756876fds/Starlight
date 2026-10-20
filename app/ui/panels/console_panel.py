"""控制台面板：后台日志投射 + 降噪分级 + 节流渲染。

设计（用户批准，2026-10-20）：
- 日志源：app/core/log_bus.LogBus.sig_log（TeeStream 每行已带 [HH:MM:SS] 前缀）。
  信号为 Qt Signal，跨线程 emit 自动队列到主线程，不在 print 调用点碰 UI。
- 降噪：只放行 4 类关键信息（启动进度 / 用户操作 / 关键报错 / 对局事件），
  噪音（QPropertyAnimation、底层轮询等）直接丢弃。
- 分级：red（报错）/ yellow（警告）/ green（成功·正常）/ white（普通）。
- 节流：模块级线程安全 deque(maxlen=200) + Lock；QTimer 500ms 批量 drain，
  合并为一段 HTML 一次 appendHtml——高频日志不卡主线程。
- 上限：最多保留 200 行，超出自动删最旧。
"""
import html
import threading
from collections import deque

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget,
)

from app.core.log_bus import get_log_bus

MAX_LINES = 200
FLUSH_INTERVAL_MS = 500

# ---- 噪音关键词（命中即丢弃，优先级高于白名单） ----
# 注意：仅影响控制台 Tab 的 UI 显示层；logs/ 落盘的完整日志不受影响。
_NOISE_KEYWORDS = (
    # 底层动画/绘制警告
    "QPropertyAnimation", "setStartValue", "setEndValue", "hover_t",
    "viewport().update", "paint(", "Shimmer",
    # 底层轮询/心跳
    "心跳", "poll ", "轮询",
    # Combo 键盘底层心跳与重连（连招执行成功类日志不在黑名单，可保留）
    "键盘监听疑似掉线", "自动重连", "ComboManager 已启动",
    # UI 内部切换（对用户无信息量）
    "[UI] switch_to_tabs", "[UI] switched",
    # 资源扫描底层细节（mutagen 读取失败过于冗长；[Scanner] 可播验证失败保留）
    "mutagen 读取失败",
)

# ---- 只放行 4 类关键信息（allowlist） ----
_KEEP_PATTERNS = (
    # 1) 启动进度
    "[0/", "[1/", "[2/", "[3/", "[4/", "[5/", "[6/", "[7/", "[8/", "[9/",
    "初始化", "已就绪", "就绪", "启动", "boot", "switch_to_tabs", "[UI] switched",
    "[ASR]", "[TTS]",
    # 2) 用户操作
    "[Voice]", "识别", "[Intent]", "意图", "[Music] 播放", "[Music] 在线",
    "播放", "在线找到", "[Movie] 播放", "解析成功", "解析失败",
    "[Interrupt",
    # 3) 关键报错（含警告级）
    "Error", "Exception", "失败", "重连", "Traceback", "错误", "异常",
    "[WARN]", "WARN", "未连接", "降级", "超时", "timeout", "不可用",
    # 4) 对局事件
    "[Ongoing]", "[GameAlarm]", "[LCU]", "[Live]", "对局", "击杀", "[Combo]",
)

_LEVEL_RULES = (
    # (命中关键词, 级别) —— 顺序即优先级：错误 > 警告 > 成功 > 普通
    ("red", ("[Error]", "Exception", "Traceback", "失败", "重连失败",
             "解析失败", "错误", "异常", "启动失败", "ERROR", "FATAL")),
    ("yellow", ("警告", "WARN", "warn", "降级", "fallback", "未连接",
                "skip", "超时", "timeout", "不可用", "unavailable",
                "已忽略", "not found")),
    ("green", ("已就绪", "就绪", "完成", "成功", "加载完成", "开始",
               "识别", "意图", "OK", "ok", "正常", "切换", "found",
               "在线找到", "解析成功", "播放")),
)


def classify_log(text: str):
    """过滤 + 分级。返回 (是否保留, 级别)；丢弃返回 (False, None)。

    级别：'red' | 'yellow' | 'green' | 'white'。
    """
    for kw in _NOISE_KEYWORDS:
        if kw in text:
            return False, None
    if not any(p in text for p in _KEEP_PATTERNS):
        return False, None
    for level, kws in _LEVEL_RULES:
        if any(kw in text for kw in kws):
            return True, level
    return True, "white"


class ConsolePanel(QWidget):
    """控制台 Tab：只读日志区 + 清空按钮。"""

    _queue = deque(maxlen=MAX_LINES)   # 线程安全入队（后台线程写）
    _qlock = threading.Lock()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._count = 0
        self._build_ui()
        # 日志总线订阅：TeeStream 每行 emit sig_log（跨线程自动排队到主线程）
        try:
            get_log_bus().sig_log.connect(self._on_log_line)
        except Exception:
            pass
        # 节流：500ms 批量刷新，防高频日志刷 UI
        self._timer = QTimer(self)
        self._timer.setInterval(FLUSH_INTERVAL_MS)
        self._timer.timeout.connect(self._drain)
        self._timer.start()

    # ---- UI ----
    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        title = QLabel("后台日志（仅显示关键信息，自动降噪）")
        title.setStyleSheet("color:#8a8f98; font-size:12px;")
        top.addWidget(title)
        top.addStretch()
        self.btn_clear = QPushButton("清空日志")
        self.btn_clear.setObjectName("console_clear")
        self.btn_clear.setFixedWidth(88)
        self.btn_clear.clicked.connect(self.clear_log)
        top.addWidget(self.btn_clear)
        root.addLayout(top)
        self.view = QPlainTextEdit()
        self.view.setReadOnly(True)
        self.view.setMaximumBlockCount(MAX_LINES)  # 上限兜底：超 200 行自动删最旧
        self.view.setStyleSheet(
            "QPlainTextEdit{background:#14161a; color:#c9cdd4; border:1px solid #2a2e35;"
            "border-radius:8px; font-family:'Cascadia Mono','Consolas',monospace; font-size:12px;}"
        )
        root.addWidget(self.view, 1)

    # ---- 日志入队（主线程槽，sig_log 跨线程连接安全） ----
    def _on_log_line(self, line: str):
        keep, level = classify_log(line)
        if not keep:
            return
        with self._qlock:
            ConsolePanel._queue.append((line, level))

    def _drain(self):
        """QTimer 槽：批量取队列 → 合并为一段 HTML → 一次渲染。"""
        if not ConsolePanel._queue:
            return
        with self._qlock:
            items = list(ConsolePanel._queue)
            ConsolePanel._queue.clear()
        if not items:
            return
        # 合并 HTML：绿色=正常成功、黄色=警告、红色=报错、白=普通
        parts = []
        for line, level in items:
            color = {
                "red": "#ff6b6b", "yellow": "#e6c07b",
                "green": "#7ee2a8", "white": "#c9cdd4",
            }.get(level, "#c9cdd4")
            parts.append(f'<span style="color:{color}">{html.escape(line)}</span>')
        self.view.appendHtml("<br/>".join(parts))
        self._count += len(items)
        # 跟随最新日志
        sb = self.view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def clear_log(self):
        with self._qlock:
            ConsolePanel._queue.clear()
        self.view.clear()
        self._count = 0

    # ---- 供测试直接投喂 ----
    @classmethod
    def feed_for_test(cls, line: str, level: str = "white"):
        with cls._qlock:
            cls._queue.append((line, level))
