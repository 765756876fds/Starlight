"""识曲弹窗：8 秒倒计时录音 → 识别中 → 结果卡片（歌名/歌手/立即播放）。

线程模型（对齐 movie_source_dialog 的 QThread 惯例）：
- 录音+识别在 _IdentifyWorker（后台 QThread），主线程 QTimer 展示倒计时；
- 用户明确点击“建立/更新指纹库”后由 _BuildWorker 后台建库；
- 防重入：_busy 标志 + thread.isRunning 检查（运行中绝不覆盖线程引用）；
- 关闭窗口仅 hide（不销毁运行中的线程；下次 start 时 isRunning 拒绝）。
"""
import os
import logging
import tempfile
import threading

from PySide6.QtCore import QObject, QThread, QTimer, Signal, Slot
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QPushButton

from ...music.capture import CaptureCancelled
from ...music.shazamio_provider import ShazamRecognitionStatus, ShazamRecognitionResult
from ..theme.colors import Colors
from ..widgets.glass_dialog import GlassDialog
from ..widgets.icon_utils import lucide_icon

logger = logging.getLogger(__name__)


class _IdentifyWorker(QObject):
    """后台：录 8 秒 → 识别。done(result dict | None)，failed(msg)。"""

    done = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, service, wav_path, source, cancel_event):
        super().__init__()
        self._service = service
        self._wav_path = wav_path
        self._source = source
        self._cancel_event = cancel_event

    @Slot()
    def run(self):
        try:
            result = self._service.record_and_identify(
                self._wav_path, source=self._source, cancel_event=self._cancel_event)
        except CaptureCancelled:
            self.cancelled.emit()
            return
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


class _OnlineIdentifyWorker(QObject):
    """后台在线识曲；provider 本身负责超时和网络资源关闭。"""

    done = Signal(object)

    def __init__(self, wav_path, provider):
        super().__init__()
        self._wav_path = wav_path
        self._provider = provider

    @Slot()
    def run(self):
        try:
            result = self._provider.recognize(self._wav_path)
        except Exception as exc:
            logger.error("online_identify worker_failed type=%s", type(exc).__name__)
            result = ShazamRecognitionResult(
                ShazamRecognitionStatus.SERVICE_ERROR,
                detail="在线识曲暂时无法完成，请重试并查看诊断结果")
        self.done.emit(result)


class IdentifyDialog(GlassDialog):
    """听歌识曲窗口（也由语音"听听这是什么歌"触发）。"""

    def __init__(self, service, on_play=None, on_search=None, on_begin=None, parent=None):
        super().__init__("识曲", parent)
        self.resize(520, 360)
        self.setMinimumSize(460, 320)
        self._service = service
        self._on_play = on_play
        self._on_search = on_search
        self._on_begin = on_begin
        self._busy = False
        self._result = None
        self._countdown = 0
        self._timer = None
        self._identify_thread = None
        self._build_thread = None
        self._source = "system"
        self._cancel_event = None
        self._current_wav = None
        self._online_thread = None
        self._online_cancelled = False

        root = self.body_layout
        self.lbl_title = QLabel("选择声音来源后，点击“开始识曲”")
        self.lbl_title.setStyleSheet("font-size: 18px; font-weight: 600;")
        self.lbl_title.setWordWrap(True)
        root.addWidget(self.lbl_title)
        self.source_combo = QComboBox()
        self.source_combo.addItem("电脑系统声音", "system")
        self.source_combo.addItem("麦克风环境音", "microphone")
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        root.addWidget(self.source_combo)
        self.lbl_status = QLabel("准备就绪")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setMinimumHeight(52)
        root.addWidget(self.lbl_status)
        action_row = QHBoxLayout()
        action_row.setSpacing(10)
        self.btn_start = QPushButton("开始识曲")
        self.btn_start.clicked.connect(self.begin)
        action_row.addWidget(self.btn_start)
        self.btn_cancel = QPushButton("取消识曲")
        self.btn_cancel.clicked.connect(self.cancel)
        self.btn_cancel.setEnabled(False)
        action_row.addWidget(self.btn_cancel)
        self.btn_play = QPushButton("立即播放")
        self.btn_play.setIcon(lucide_icon("play", 14, Colors.TEXT_PRIMARY))
        self.btn_play.hide()
        self.btn_play.clicked.connect(self._on_play_clicked)
        action_row.addWidget(self.btn_play)
        root.addLayout(action_row)
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
        """只初始化窗口，不开始录音。"""
        if self._busy:
            return
        self._result = None
        self.btn_play.hide()
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self._on_source_changed(self.source_combo.currentIndex())

    def begin(self):
        """用户明确点击后才检查指纹库并开始采集。"""
        if self._busy or (self._identify_thread is not None and self._identify_thread.isRunning()):
            return
        try:
            if not self._service.has_library():
                self.lbl_title.setText("本地指纹库为空")
                self.lbl_status.setText("请先点击“建立/更新指纹库”，完成后再开始识曲")
                return
        except Exception as e:
            self.lbl_status.setText(f"指纹库不可用：{e}")
            return
        self._begin_record()

    def is_busy(self) -> bool:
        return self._busy

    # ------------------------------------------------------------------ 录音/识别
    def _begin_record(self):
        if self._on_begin:
            self._on_begin()
        self._busy = True
        self._cancel_event = threading.Event()
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
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.source_combo.setEnabled(False)
        wav_path = os.path.join(tempfile.gettempdir(), f"humming_{os.getpid()}.wav")
        self._current_wav = wav_path
        self._start_identify_worker(wav_path, self._source, self._cancel_event)

    def _start_identify_worker(self, wav_path, source="microphone", cancel_event=None):
        self._identify_thread = QThread()
        self._identify_worker = _IdentifyWorker(
            self._service, wav_path, source, cancel_event or threading.Event())
        self._identify_worker.moveToThread(self._identify_thread)
        self._identify_thread.started.connect(self._identify_worker.run)
        self._identify_worker.done.connect(self._on_done)
        self._identify_worker.failed.connect(self._on_failed)
        self._identify_worker.cancelled.connect(self._on_cancelled)
        self._identify_worker.done.connect(self._identify_thread.quit)
        self._identify_worker.failed.connect(self._identify_thread.quit)
        self._identify_worker.cancelled.connect(self._identify_thread.quit)
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
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.source_combo.setEnabled(True)
        self._result = result
        if self._timer is not None:
            self._timer.stop()
        if result is None:
            self.lbl_title.setText("识曲")
            self._start_online_identify()
            return
        self._cleanup_wav()
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

    def _start_online_identify(self):
        """本地正常未命中后自动在线识曲（仅由本次主动识曲触发）。"""
        try:
            from ...music.shazamio_provider import ShazamIOProvider
            self._busy = True
            self._online_cancelled = False
            self.btn_start.setEnabled(False)
            self.source_combo.setEnabled(False)
            self.btn_cancel.setEnabled(True)
            self._online_thread = QThread()
            self._online_worker = _OnlineIdentifyWorker(self._current_wav, ShazamIOProvider())
            self._online_worker.moveToThread(self._online_thread)
            self._online_thread.started.connect(self._online_worker.run)
            self._online_worker.done.connect(self._on_online_done)
            self._online_worker.done.connect(self._online_thread.quit)
            self._online_thread.finished.connect(self._cleanup_wav)
            self._online_thread.start()
            logger.info("online_identify_started")
            self.lbl_title.setText("正在在线识曲…")
            self.lbl_status.setText("已同意发送短音频，正在等待识别结果")
        except Exception as exc:
            self._cleanup_wav()
            self._on_failed(f"在线识曲初始化失败：{exc}")

    @Slot(object)
    def _on_online_done(self, result):
        self.btn_start.setEnabled(True)
        self.source_combo.setEnabled(True)
        if self._online_cancelled:
            self._busy = False
            self.btn_cancel.setEnabled(False)
            self._cleanup_wav()
            self.lbl_title.setText("在线识曲已取消")
            self.lbl_status.setText("已取消在线识曲，不会使用返回结果")
            return
        self._busy = False
        self.btn_cancel.setEnabled(False)
        logger.info("online_identify_result status=%s", result.status.value)
        if result.status is not ShazamRecognitionStatus.SUCCESS:
            self._cleanup_wav()
            messages = {
                ShazamRecognitionStatus.NO_MATCH: "在线服务未找到匹配歌曲，请播放清晰的歌曲片段后重试",
                ShazamRecognitionStatus.TIMEOUT: "在线识曲超时，请检查网络后重试",
                ShazamRecognitionStatus.NETWORK_ERROR: "在线识曲连接失败，请检查网络后重试",
                ShazamRecognitionStatus.RATE_LIMITED: "在线识曲请求过于频繁，请稍后再试",
                ShazamRecognitionStatus.DEPENDENCY_MISSING: "在线识曲组件加载失败，请打开设置 → 诊断，复制检查结果",
            }
            self.lbl_title.setText("识曲")
            self.lbl_status.setText(messages.get(result.status, result.detail or "在线识曲失败"))
            return
        self._cleanup_wav()
        result_data = {"title": result.title, "artist": result.artist,
                       "source": "shazamio", "track_id": result.track_id, "url": result.url}
        self._result = result_data
        self.lbl_title.setText(f"在线识别到：{result.title} - {result.artist}")
        if self._on_search:
            try:
                self._on_search(f"{result.title} {result.artist}")
                self.lbl_status.setText("已识别，正在搜索可用音源；不会自动播放")
            except Exception as exc:
                self.lbl_status.setText(f"歌曲已识别，但在线音源搜索失败：{exc}")
        else:
            self.lbl_status.setText("已识别，未连接在线音源搜索；不会自动播放")

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
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.source_combo.setEnabled(True)
        if self._timer is not None:
            self._timer.stop()
        self._cleanup_wav()
        self.lbl_title.setText("识曲")
        self.lbl_status.setText(f"识别失败：{msg}")

    @Slot()
    def _on_cancelled(self):
        self._busy = False
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.source_combo.setEnabled(True)
        if self._timer is not None:
            self._timer.stop()
        self._cleanup_wav()
        self.lbl_title.setText("识曲已取消")
        self.lbl_status.setText("已取消识曲，音频设备已释放，可以重新开始")

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
        scanned = int(getattr(self._service, "last_scan_count", 0) or 0)
        self.lbl_status.setText(f"指纹库更新完成：新增 {indexed} 首，跳过 {skipped} 首，失败 {failed} 首")
        if not self._service.has_library():
            if scanned == 0:
                self.lbl_title.setText("未发现可扫描歌曲")
                self.lbl_status.setText("请先在设置中添加并启用音乐目录，再建立指纹库")
            elif failed:
                self.lbl_title.setText("指纹库未就绪")
                self.lbl_status.setText(
                    f"扫描到 {scanned} 首歌曲，但全部指纹计算失败（失败 {failed} 首），请检查音频格式和 FFmpeg")
            else:
                self.lbl_title.setText("指纹库未就绪")
                self.lbl_status.setText("未生成可用于识曲的指纹记录，请检查音乐文件")
        else:
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

    def cancel(self):
        if self._online_thread is not None and self._online_thread.isRunning():
            self._online_cancelled = True
            self.btn_cancel.setEnabled(False)
            self.lbl_status.setText("正在取消在线识曲…")
            return
        if not self._busy or self._cancel_event is None:
            return
        self._cancel_event.set()
        self.btn_cancel.setEnabled(False)
        self.lbl_status.setText("正在取消识曲并释放音频设备…")

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
        path = self._current_wav or os.path.join(tempfile.gettempdir(), f"humming_{os.getpid()}.wav")
        try:
            os.remove(path)
        except OSError:
            pass
        self._current_wav = None

    def closeEvent(self, event):
        # 运行中的线程引用保留（等自然结束），只隐藏窗口，避免 QThread 销毁崩溃
        self.hide()
        event.ignore()
