"""
Highlight Detector - 统一入口

默认算法：V4 Early-Rise（RMS 能量峰 + 上升沿回溯）。
历史上 pychorus 作为主算法，但实听发现其条带起点偏晚 1~3s，且会选中
非高潮的重复段；2026-10 恢复为老版本 V4 Early-Rise 作为默认。
`_try_pychorus()` 保留为可选实验函数，不再作为默认路径。

任何异常都不向上抛：音乐播放链不允许因为高潮检测失败而中断。
返回 (start_seconds, end_seconds, score)；失败返回 None。
"""
import contextlib
import io
import logging
import math
import os
import threading
import warnings

import numpy as np

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = "v4_early_rise"
SOURCE_TAG_PYCHORUS = "pychorus"  # 保留常量，老库中可能存在
SOURCE_TAG_V4 = "local_v4_early_rise"

# 每线程保存"本次检测实际使用的算法 source"，避免多线程并发时互相覆盖。
# scanner 当前单线程，但接口对外暴露，未来并发调用也安全。
_tls = threading.local()

# 片段长度目标（秒）：pychorus 以 clip_length 切窗；V4 有自己的边界
TARGET_CLIP_S = 35.0
MIN_DURATION_S = 15.0
MAX_DURATION_S = 75.0
EDGE_MARGIN_S = 5.0
# 同时满足"片段≥15s"和"两端各留 5s"的最短歌曲时长
MIN_SONG_FOR_MARGINED_SEGMENT = MIN_DURATION_S + 2 * EDGE_MARGIN_S  # 25s


def _silent_load(audio_path: str):
    """静默加载音频（坏文件解码器会向 fd 2 打报错）。"""
    import librosa  # 惰性加载

    with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore")
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
            y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=600)
            duration = librosa.get_duration(y=y, sr=sr)
            return y, sr, duration
        finally:
            os.dup2(saved, 2)
            os.close(devnull)
            os.close(saved)


def _is_finite(x) -> bool:
    """拒绝 None / NaN / inf / 非数值。"""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def _validate_segment(start: float, end: float, duration: float) -> tuple[float, float] | None:
    """统一边界校验。合法时返回 (start, end)，否则 None。

    规则：
    - start/end 必须是有限数；start>=0, end>start, end<=duration（duration>0 时）。
    - 片段时长在 [MIN_DURATION_S, MAX_DURATION_S]。
    - 若歌曲时长足够（>=25s），要求距首尾各 >=EDGE_MARGIN_S。
    - 歌曲过短（<25s）无法同时满足"≥15s 片段+两端 5s"，直接判失败，不裁剪出越界/负起点。
    """
    if not _is_finite(start) or not _is_finite(end):
        return None
    start_f = float(start)
    end_f = float(end)
    if start_f < 0 or end_f <= start_f:
        return None

    if duration and duration > 0:
        if end_f > duration + 1e-3:
            return None
        if start_f >= duration - 1e-3:
            return None

    seg = end_f - start_f
    if seg < MIN_DURATION_S - 1e-3 or seg > MAX_DURATION_S + 1e-3:
        return None

    # 边缘保护：仅在歌曲足够长时施加
    if duration and duration >= MIN_SONG_FOR_MARGINED_SEGMENT:
        if start_f < EDGE_MARGIN_S - 1e-3:
            return None
        if end_f > duration - EDGE_MARGIN_S + 1e-3:
            return None
    # 歌曲过短（<25s）：无法同时满足 15s 片段 + 两端 5s，直接失败
    elif duration and duration < MIN_SONG_FOR_MARGINED_SEGMENT:
        return None

    return (round(start_f, 2), round(end_f, 2))


# ---------------------------------------------------------------------------
# 主算法：pychorus
# ---------------------------------------------------------------------------
def _try_pychorus(audio_path: str) -> tuple[float, float] | None:
    """用 pychorus 找重复段落。返回 (start, end)，失败返回 None。"""
    try:
        from pychorus import find_and_output_chorus  # 惰性：未装时 ImportError
    except Exception as e:
        logger.debug(f"[highlight] pychorus 不可用，回退 V4: {e}")
        return None

    try:
        start = find_and_output_chorus(audio_path, None, clip_length=TARGET_CLIP_S)
    except Exception as e:
        logger.debug(f"[highlight] pychorus 执行失败: {e}")
        return None

    if not _is_finite(start):
        return None
    start_f = float(start)
    if start_f < 0:
        return None

    end_f = start_f + TARGET_CLIP_S

    # 歌曲真实时长：再读一次短元数据，做边界校验
    try:
        import librosa
        duration = librosa.get_duration(path=audio_path)
    except Exception:
        duration = 0.0

    # 越界尝试对齐（只在歌曲足够长时才允许对齐到尾部/头部）
    if duration and duration >= MIN_SONG_FOR_MARGINED_SEGMENT:
        if end_f > duration - EDGE_MARGIN_S:
            end_f = min(end_f, duration - EDGE_MARGIN_S)
            start_f = end_f - TARGET_CLIP_S
        if start_f < EDGE_MARGIN_S:
            start_f = EDGE_MARGIN_S
            end_f = start_f + TARGET_CLIP_S

    # 统一校验（含短歌失败）
    return _validate_segment(start_f, end_f, duration)


# ---------------------------------------------------------------------------
# 备用：V4 Early-Rise（RMS 能量峰）
# ---------------------------------------------------------------------------
def _smooth(x: np.ndarray, win: int = 11) -> np.ndarray:
    kernel = np.ones(win) / win
    return np.convolve(x, kernel, mode='same')


def _try_v4(audio_path: str) -> tuple[float, float, float] | None:
    try:
        from scipy.signal import find_peaks  # 惰性
        import librosa
        y, sr, duration = _silent_load(audio_path)

        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)

        rms_s = _smooth(rms, win=11)
        rms_n = (rms_s - rms_s.min()) / (rms_s.max() - rms_s.min() + 1e-8)

        peaks, props = find_peaks(rms_n, height=0.6, distance=int(10 / (times[1] - times[0])))
        if len(peaks) == 0:
            return None

        best_peak = peaks[np.argmax(props["peak_heights"])]
        peak_val = rms_n[best_peak]

        rise_thresh = peak_val * 0.35
        left_idx = best_peak
        while left_idx > 0 and rms_n[left_idx] > rise_thresh:
            left_idx -= 1
        fall_thresh = peak_val * 0.3
        right_idx = best_peak
        while right_idx < len(rms_n) - 1 and rms_n[right_idx] > fall_thresh:
            right_idx += 1

        start = float(times[left_idx])
        end = float(times[right_idx])

        # 片段太短/太长时，用峰中心 ±25s 兜底
        if not (MIN_DURATION_S <= end - start <= MAX_DURATION_S):
            center = float(times[best_peak])
            start = max(0.0, center - 25.0)
            end = min(duration, center + 25.0)

        # 边缘 clamp（仅在歌曲足够长时）
        if duration >= MIN_SONG_FOR_MARGINED_SEGMENT:
            start = max(EDGE_MARGIN_S, start)
            end = min(duration - EDGE_MARGIN_S, end)

        # 统一边界校验（含短歌失败）
        seg = _validate_segment(start, end, duration)
        if seg is None:
            return None
        return (seg[0], seg[1], round(float(peak_val), 3))
    except Exception as e:
        logger.debug(f"[highlight] V4 失败 {audio_path}: {e}")
        return None


# ---------------------------------------------------------------------------
# 对外统一接口
# ---------------------------------------------------------------------------
def detect_highlight(audio_path: str) -> tuple[float, float, float] | None:
    """统一入口：默认走 V4 Early-Rise。

    返回 (start_seconds, end_seconds, score)；失败返回 None。
    score：V4 用归一化峰高。
    """
    if not audio_path or not os.path.exists(audio_path):
        return None

    # 每次检测开始重置本线程 source（避免上一次结果泄漏）
    _tls.last_source = SOURCE_TAG_V4

    return _try_v4(audio_path)


def pop_last_source() -> str:
    """返回当前线程最近一次 detect_highlight 实际使用的算法 source tag。

    基于 threading.local，多线程并发调用不会互相覆盖。
    若本线程尚未调用过 detect_highlight，返回默认 V4 tag。"""
    return getattr(_tls, "last_source", SOURCE_TAG_V4)


def get_algorithm_version() -> str:
    return ALGORITHM_VERSION


def get_source_tag() -> str:
    """scanner 用这个 source 字段判断"是否已用当前算法算过"。

    当前主算法是 V4 Early-Rise；老库中 source='pychorus' 的记录会被
    scanner 识别为旧算法产物，触发迁移重算。"""
    return SOURCE_TAG_V4
