"""
Highlight Detector - V4 Early-Rise 本地高光检测
纯CPU后台运行，失败不影响任何核心功能
P1（2026-10-18）：librosa 惰性加载（移入函数内）——避免启动链必载
numba/llvmlite，降低打包难度与启动开销。
"""
import contextlib
import io
import logging
import warnings

import numpy as np
from scipy.signal import find_peaks

logger = logging.getLogger(__name__)

ALGORITHM_VERSION = "v4_early_rise"
SOURCE_TAG = "local_v4_early_rise"

# 合理边界
MIN_DURATION_S = 15
MAX_DURATION_S = 75
EDGE_MARGIN_S = 5

def smooth(x: np.ndarray, win: int = 11) -> np.ndarray:
    kernel = np.ones(win) / win
    return np.convolve(x, kernel, mode='same')

def _silent_load(audio_path: str):
    """静默加载音频：坏/损坏文件的解码器会向 stderr 打印报错
    （如 mpg123 子进程的 junk 报错，走 fd 2，Python 层 redirect_stderr 拦不住）。
    这里在 load 期间把进程 fd 2 临时指向 devnull，加载完立即恢复；
    同时抑制 librosa 的 UserWarning/FutureWarning。窗口仅限本次解码调用。"""
    import os as _os

    import librosa  # 惰性：仅高光检测实际调用时加载（P1）
    with warnings.catch_warnings(), contextlib.redirect_stderr(io.StringIO()):
        warnings.simplefilter("ignore")
        saved = _os.dup(2)
        devnull = _os.open(_os.devnull, _os.O_WRONLY)
        try:
            _os.dup2(devnull, 2)
            y, sr = librosa.load(audio_path, sr=22050, mono=True, duration=600)  # 最多读10分钟
            duration = librosa.get_duration(y=y, sr=sr)
            return y, sr, duration
        finally:
            _os.dup2(saved, 2)
            _os.close(devnull)
            _os.close(saved)


def detect_highlight(audio_path: str) -> tuple[float, float, float] | None:
    """
    检测歌曲高光位置
    返回 (start_seconds, end_seconds, score)，失败返回None
    """
    try:
        import librosa  # 惰性（P1）
        # 静默加载（坏文件解码器 stderr / librosa 警告均不刷控制台）
        y, sr, duration = _silent_load(audio_path)
        
        hop = 512
        rms = librosa.feature.rms(y=y, hop_length=hop)[0]
        times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop)
        
        # 平滑
        rms_s = smooth(rms, win=11)
        rms_n = (rms_s - rms_s.min()) / (rms_s.max() - rms_s.min() + 1e-8)
        
        # 找能量峰
        peaks, props = find_peaks(rms_n, height=0.6, distance=int(10/(times[1]-times[0])))
        
        if len(peaks) == 0:
            return None
        
        # 取最高的峰
        best_peak = peaks[np.argmax(props["peak_heights"])]
        peak_val = rms_n[best_peak]
        
        # V4 Early-Rise: 从峰值35%上升处开始，30%下降处结束
        rise_thresh = peak_val * 0.35
        left_idx = best_peak
        while left_idx > 0 and rms_n[left_idx] > rise_thresh:
            left_idx -= 1
        
        fall_thresh = peak_val * 0.3
        right_idx = best_peak
        while right_idx < len(rms_n)-1 and rms_n[right_idx] > fall_thresh:
            right_idx += 1
        
        start = float(times[left_idx])
        end = float(times[right_idx])
        dur = end - start
        
        # 边界检查
        if dur < MIN_DURATION_S or dur > MAX_DURATION_S:
            # 不合法，截断成中心±25秒
            center = float(times[best_peak])
            start = max(0, center - 25)
            end = min(duration, center + 25)
            dur = end - start
            if dur < MIN_DURATION_S:
                return None
        
        # 边缘保护
        start = max(EDGE_MARGIN_S, start)
        end = min(duration - EDGE_MARGIN_S, end)
        
        return (round(start, 2), round(end, 2), round(float(peak_val), 3))
    
    except Exception as e:
        # 检测失败静默处理：只留 debug 记录，不刷控制台
        logger.debug(f"Highlight检测失败 {audio_path}: {e}")
        return None

def get_algorithm_version() -> str:
    return ALGORITHM_VERSION

def get_source_tag() -> str:
    return SOURCE_TAG
