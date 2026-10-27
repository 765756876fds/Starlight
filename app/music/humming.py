"""哼唱识曲服务：基于 audiofp（Shazam 式音频指纹）的本地听歌识曲。

- 建库：扫描 music.scan_dirs 下的音频 → 指纹入库（增量：已入库文件跳过）。
- 识别：录 8 秒麦克风音频 → 临时 wav → Runtime.search_file → 匹配结果。

依赖说明：audiofp 源码随仓库放在 app/third_party/fingerprint（一键迁移无需重装
pip 包）；运行依赖 numpy/scipy/soundfile/soxr 见 requirements.txt。
"""
import os
import sys
import time
import wave

# ---- third_party 加载（不依赖 main.py 改 sys.path，红线：不碰 main.py）----
_THIRD_PARTY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "third_party")
if os.path.isdir(_THIRD_PARTY) and _THIRD_PARTY not in sys.path:
    sys.path.insert(0, _THIRD_PARTY)

# 故障隔离：audiofp 缺失（third_party 被删/换机器未同步）→ 标记不可用，不崩
try:
    from fingerprint.api.runtime import Runtime
    from fingerprint.config import Settings
    from fingerprint.core.matcher import quality_label
    _AUDIOFP_AVAILABLE = True
except ImportError:
    Runtime = Settings = quality_label = None
    _AUDIOFP_AVAILABLE = False

# 默认支持的音频扩展（audiofp 原生 libsndfile 可解 flac/mp3/wav/ogg/opus/aiff；
# m4a/aac 需 ffmpeg，作为可选不阻塞）
DEFAULT_EXTENSIONS = [".mp3", ".flac", ".wav", ".ogg", ".opus", ".aiff"]

RECORD_SECONDS = 8        # 录音时长
RECORD_SAMPLE_RATE = 16000  # 对齐项目 voice 链的采样率


class HummingError(Exception):
    """哼唱识曲业务异常（建库/录音/识别失败统一出口）。"""


def _pyaudio_record(out_path: str, seconds: int = RECORD_SECONDS,
                    sample_rate: int = RECORD_SAMPLE_RATE, chunk: int = 1024) -> None:
    """用 PyAudio（项目既有依赖）录 `seconds` 秒单声道 16bit PCM → wav 文件。"""
    try:
        import pyaudio
    except ImportError as e:
        import sys as _sys
        raise HummingError(
            f"pyaudio 导入失败: {e}；当前 Python={_sys.executable}；"
            f"请用 .venv\\Scripts\\python.exe 启动并 pip install -r requirements.txt"
        )
    pa = pyaudio.PyAudio()
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=sample_rate,
            input=True,
            frames_per_buffer=chunk,
        )
        frames = []
        total_frames = int(seconds * sample_rate / chunk)
        for _ in range(total_frames):
            frames.append(stream.read(chunk, exception_on_overflow=False))
        stream.stop_stream()
        stream.close()
    finally:
        pa.terminate()
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(b"".join(frames))


class HummingService:
    """AudioFP 指纹库 + 识别服务（线程安全：SQLiteStore 每线程独立连接）。"""

    def __init__(self, scan_dirs: list[str], sqlite_path: str,
                 extensions: list[str] | None = None,
                 record_seconds: int = RECORD_SECONDS):
        if not _AUDIOFP_AVAILABLE:
            raise HummingError("audiofp 未就绪：缺少 app/third_party/fingerprint，请同步仓库后重试")
        self.scan_dirs = [d for d in scan_dirs if d]
        self.extensions = [e.lower() if not e.startswith(".") else e.lower()
                           for e in (extensions or DEFAULT_EXTENSIONS)]
        self.record_seconds = max(3, int(record_seconds))
        db_dir = os.path.dirname(os.path.abspath(sqlite_path)) or "."
        os.makedirs(db_dir, exist_ok=True)
        self.settings = Settings(sqlite_path=os.path.abspath(sqlite_path), data_dir=db_dir)
        self._runtime: Runtime | None = None

    # ------------------------------------------------------------------ runtime
    @property
    def runtime(self) -> Runtime:
        """惰性创建 Runtime（Fingerprinter/Matcher/Store 构造有成本，复用单例）。"""
        if self._runtime is None:
            self._runtime = Runtime(self.settings)
        return self._runtime

    def close(self) -> None:
        if self._runtime is not None:
            try:
                self._runtime.close()
            except Exception:
                pass
            self._runtime = None

    # ------------------------------------------------------------------ 建库
    def _scan_audio(self) -> list[str]:
        """递归扫描 scan_dirs 下支持的音频文件。"""
        found: list[str] = []
        for root in self.scan_dirs:
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirnames, filenames in os.walk(root):
                for name in filenames:
                    if os.path.splitext(name)[1].lower() in self.extensions:
                        found.append(os.path.abspath(os.path.join(dirpath, name)))
        return sorted(found)

    def index_pending(self) -> dict[str, int]:
        """增量建库：已入库（按绝对路径 content 指纹判重）的文件跳过，只建新增。

        返回 {indexed, skipped, failed}。skipped = 已入库 + 本次重复跳过。
        """
        paths = self._scan_audio()
        if not paths:
            return {"indexed": 0, "skipped": 0, "failed": 0}
        rt = self.runtime
        store = rt.storage
        pending = [p for p in paths if store.find_by_filepath(p) is None]
        if not pending:
            return {"indexed": 0, "skipped": len(paths), "failed": 0}
        summary = rt.indexer.index_paths(pending, source="humming")
        store.flush()
        indexed = int(summary.indexed)
        failed = int(summary.failed)
        return {"indexed": indexed, "skipped": len(paths) - indexed - failed, "failed": failed}

    def has_library(self) -> bool:
        try:
            return self.runtime.storage.count_tracks() > 0
        except Exception:
            return False

    # ------------------------------------------------------------------ 识别
    def identify_file(self, wav_path: str) -> dict[str, object] | None:
        """识别一段音频文件，返回首名匹配（无命中返回 None）。

        返回：{title, artist, filepath, filename, confidence, aligned, peak_ratio, quality, offset_sec}
        """
        if not os.path.isfile(wav_path):
            raise HummingError(f"音频文件不存在: {wav_path}")
        result = self.runtime.search_file(wav_path)
        if not result.matches:
            return None
        m = result.matches[0]
        tr = m.track
        return {
            "title": (tr.title if tr else "") or "",
            "artist": (tr.artist if tr else "") or "",
            "filepath": (tr.filepath if tr else "") or "",
            "filename": (tr.filename if tr else "") or "",
            "confidence": float(m.confidence),
            "aligned": int(m.aligned_hashes),
            "peak_ratio": float(m.peak_ratio),
            "quality": quality_label(m.confidence, m.peak_ratio),
            "offset_sec": float(m.best.offset_sec),
        }

    def record_and_identify(self, wav_path: str) -> dict[str, object] | None:
        """录 `record_seconds` 秒麦克风 → 临时 wav → 识别。供后台线程调用。"""
        t0 = time.perf_counter()
        _pyaudio_record(wav_path, seconds=self.record_seconds)
        result = self.identify_file(wav_path)
        elapsed = time.perf_counter() - t0
        if result is not None:
            result["elapsed_sec"] = round(elapsed, 1)
        return result
