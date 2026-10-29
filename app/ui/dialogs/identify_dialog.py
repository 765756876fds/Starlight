"""识曲弹窗：8 秒倒计时录音 → 识别中 → 结果卡片（歌名/歌手/立即播放）。

线程模型（对齐 movie_source_dialog 的 QThread 惯例）：
- 录音+识别在 _IdentifyWorker（后台 QThread），主线程 QTimer 展示倒计时；
- 用户明确点击“建立/更新指纹库”后由 _BuildWorker 后台建库；
- 防重入：_busy 标志 + thread.isRunning 检查（运行中绝不覆盖线程引用）；
- 关闭窗口仅 hide（不销毁运行中的线程；下次 start 时 isRunning 拒绝）。
"""
import os
import tempfile

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from ..theme.colors import Colors
from ..widgets.glass_dialog import GlassDialog
from ..widgets.icon_utils import lucide_icon


class _IdentifyWorker(QObject):
    """后台：录 8 秒 → 识别。done(result dict | None)，failed(msg)。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, service, wav_path, source):
        super().__init__()
        self._service = service
        self._wav_path = wav_path
        self._source = source

    @Slot()
    def run(self):
        try:
            result = self._service.record_and_identify(self._wav_path, source=self._source)
        except Exception as e:  # 故障隔离：任何异常 → failed，不崩
            self.failed.emit(str(e))
            return
        self.done.emit(result)


class _BuildWorker(QObject):
    """后台：增量建库（已入库跳过）。done(stats dict)，failed(msg)。"""

    done = Signal(object)
    failed = Signal(str)

    def __init__(self, service):
        super().__init__()
        self._service = service

    @Slot()
    def run(self):
        try:
            stats = self._service.index_pending()
        except Exception as e:
            self.failed.emit(str(e))
            return
        self.done.emit(stats)


class IdentifyDialog(GlassDialog):
    """听歌识曲窗口（也由语音"听听这是什么歌"触发）。"""

    def __init__(self, service, on_play=None, parent=None):
        super().__init__("识曲", parent)
        self.resize(380, 200)
        self._service = service
        self._on_play = on_play
        self._busy = False
        self._result = None
        self._countdown = 0
        self._timer = None
        self._identify_thread = None
        self._build_thread = None
        self._source = "microphone"

        root = self.body_layout
        self.lbl_title = QLabel("把手机/音箱凑近麦克风，听 8 秒就能识别")
        self.lbl_title.setWordWrap(True)
        root.addWidget(self.lbl_title)
        self.source_combo = QComboBox()
        self.source_combo.addItem("麦克风环境音", "microphone")
        self.source_combo.addItem("电脑系统声音", "system")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        root.addWidget(self.source_combo)
        self.lbl_status = QLabel("准备就绪")
        self.lbl_status.setWordWrap(True)
        root.addWidget(self.lbl_status)
        self.btn_play = QPushButton("立即播放")
        self.btn_play.setIcon(lucide_icon("play", 14, Colors.TEXT_PRIMARY))
        self.btn_play.hide()
        self.btn_play.clicked.connect(self._on_play_clicked)
        root.addWidget(self.btn_play)
        self.btn_index = QPushButton("建立/更新指纹库")
        self.btn_index.clicked.connect(self._on_index_clicked)
        root.addWidget(self.btn_index)
        row = QHBoxLayout()
        self.btn_retry = QPushButton("重新识别")
        self.btn_retry.clicked.connect(self.start)
        row.addWidget(self.btn_retry)
        self.btn_close = QPushButton("关闭")
        self.btn_close.clicked.connect(self._on_close_clicked)
        row.addWidget(self.btn_close)
        root.addLayout(row)

    # ------------------------------------------------------------------ 对外
    def start(self):
        """开始一次识曲（防重入：录音/识别/建库中忽略重复触发）。"""
        if self._busy:
            return
        if self._identify_thread is not None and self._identify_thread.isRunning():
            return
        if self._build_thread is not None and self._build_thread.isRunning():
            return
        try:
            if not self._service.has_library():
                self._busy = False
                self.lbl_title.setText("本地指纹库为空")
                self.lbl_status.setText("请先扫描音乐库建立指纹库，再开始识曲")
                self.btn_index.show()
                return
        except Exception as e:
            self.lbl_status.setText(f"指纹库不可用：{e}")
            return
        self._begin_record()

    def is_busy(self) -> bool:
        return self._busy

    # ------------------------------------------------------------------ 录音/识别
    def _begin_record(self):
        self._busy = True
        self._result = None
        self.btn_play.hide()
        self._countdown = self._service.record_seconds
        source_name = "麦克风" if self._source == "microphone" else "系统声音"
        self.lbl_title.setText(f"正在采集{source_name}...（约 {self._countdown} 秒）")
        self.lbl_status.setText(f"采集中... {self._countdown} 秒")
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(self._on_tick)
        self._timer.start(1000)
        wav_path = os.path.join(tempfile.gettempdir(), f"humming_{os.getpid()}.wav")
        self._start_identify_worker(wav_path, self._source)

    def _start_identify_worker(self, wav_path, source="microphone"):
        self._identify_thread = QThread()
        self._identify_worker = _IdentifyWorker(self._service, wav_path, source)
        self._identify_worker.moveToThread(self._identify_thread)
        self._identify_thread.started.connect(self._identify_worker.run)
        self._identify_worker.done.connect(self._on_done)
        self._identify_worker.failed.connect(self._on_failed)
        self._identify_worker.done.connect(self._identify_thread.quit)
        self._identify_worker.failed.connect(self._identify_thread.quit)
        self._identify_thread.finished.connect(self._cleanup_wav)
        self._identify_thread.start()

    def _on_tick(self):
        self._countdown -= 1
        if self._countdown > 0:
            self.lbl_status.setText(f"录音中... {self._countdown} 秒")
        else:
            self._timer.stop()
            self.lbl_status.setText("识别中...")

    @Slot(object)
    def _on_done(self, result):
        self._busy = False
        self._result = result
        if self._timer is not None:
            self._timer.stop()
        if result is None:
            self.lbl_title.setText("识曲")
            self.lbl_status.setText("未找到匹配结果（试试更清晰的片段或换一首）")
            return
        title = result.get("title") or result.get("filename") or "未知歌曲"
        artist = result.get("artist") or ""
        conf = result.get("confidence")
        name = f"{title} - {artist}" if artist else title
        self.lbl_title.setText(f"匹配到：{name}")
        source_name = "麦克风" if result.get("source", self._source) == "microphone" else "系统声音"
        details = f"来源：{source_name}"
        if conf is not None:
            details += f"；置信度 {float(conf):.0%}（{result.get('quality', '')}）"
        filepath = result.get("filepath") or ""
        if filepath:
            details += f"；文件：{filepath}"
        if int(result.get("candidate_count", 1) or 1) > 1:
            self.lbl_status.setText(f"找到多个候选，当前未自动选择；{details}")
            self.btn_play.hide()
            return
        self.lbl_status.setText(details)
        self.btn_play.show()

    def _on_source_changed(self, _index):
        self._source = self.source_combo.currentData() or "microphone"
        if not self._busy:
            if self._source == "system":
                self.lbl_title.setText("准备采集电脑系统声音")
            else:
                self.lbl_title.setText("准备采集麦克风环境音")

    @Slot(str)
    def _on_failed(self, msg):
        self._busy = False
        if self._timer is not None:
            self._timer.stop()
        self.lbl_title.setText("识曲")
        self.lbl_status.setText(f"识别失败：{msg}")

    # ------------------------------------------------------------------ 首次建库
    def _show_build(self):
        if self._busy:
            return
        self._busy = True
        self.btn_index.setEnabled(False)
        self.btn_retry.setEnabled(False)
        self.lbl_title.setText("首次使用")
        self.lbl_status.setText("正在建立/更新本地指纹库，请稍候...")
        self._build_thread = QThread()
        self._build_worker = _BuildWorker(self._service)
        self._build_worker.moveToThread(self._build_thread)
        self._build_thread.started.connect(self._build_worker.run)
        self._build_worker.done.connect(self._on_build_done)
        self._build_worker.failed.connect(self._on_build_failed)
        self._build_worker.done.connect(self._build_thread.quit)
        self._build_worker.failed.connect(self._build_thread.quit)
        self._build_thread.start()

    @Slot(object)
    def _on_build_done(self, stats):
        self._busy = False
        self.btn_index.setEnabled(True)
        self.btn_retry.setEnabled(True)
        if self._timer is not None:
            self._timer.stop()
        indexed = stats.get("indexed", 0)
        skipped = stats.get("skipped", 0)
        failed = stats.get("failed", 0)
        self.lbl_status.setText(f"指纹库更新完成：新增 {indexed} 首，跳过 {skipped} 首，失败 {failed} 首")
        self.lbl_title.setText("可以开始识曲")
        self.btn_index.show()

    @Slot(str)
    def _on_build_failed(self, msg):
        self._busy = False
        self.btn_index.setEnabled(True)
        self.btn_retry.setEnabled(True)
        self.lbl_title.setText("识曲")
        self.lbl_status.setText(f"指纹库更新失败：{msg}")

    def _on_index_clicked(self):
        """用户明确触发后台建库，不自动开始录音。"""
        self._show_build()

    # ------------------------------------------------------------------ 播放/关闭
    def _on_play_clicked(self):
        if self._on_play and self._result:
            try:
                self._on_play(self._result)
            except Exception as e:
                print(f"[Identify] 播放回调失败: {e}")

    def _on_close_clicked(self):
        self.hide()

    def _cleanup_wav(self):
        try:
            os.remove(os.path.join(tempfile.gettempdir(), f"humming_{os.getpid()}.wav"))
        except OSError:
            pass

    def closeEvent(self, event):
        # 运行中的线程引用保留（等自然结束），只隐藏窗口，避免 QThread 销毁崩溃
        self.hide()
        event.ignore()
