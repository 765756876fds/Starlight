"""
歌曲高潮检测 v2
改进版：基于RMS能量平滑 + 结构分割
"""
import os
import subprocess
import tempfile
import numpy as np
import librosa
from typing import Tuple, Optional


def _load_audio(audio_path: str, sr: int = 22050) -> Tuple[np.ndarray, int]:
    """用mpv解码音频"""
    mpv_path = r"D:\新建文件夹 (2)\mpv.exe"
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    try:
        cmd = [mpv_path, audio_path, "--no-video", f"--o={tmp.name}", "--really-quiet", "--no-terminal"]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        y, sr = librosa.load(tmp.name, sr=sr, mono=True)
        return y, sr
    finally:
        os.unlink(tmp.name)


def detect_chorus_v2(
    audio_path: str,
    target_length: float = 45.0,  # 目标高潮长度45秒
    min_length: float = 30.0,
    max_length: float = 60.0,
    skip_start: float = 20.0,
    skip_end: float = 15.0,
) -> Tuple[Optional[float], Optional[float]]:
    """
    改进版高潮检测：
    1. 计算RMS能量曲线
    2. 平滑能量曲线
    3. 找能量最高的连续N秒窗口
    4. 跳过前奏尾奏
    """
    y, sr = _load_audio(audio_path)
    duration = librosa.get_duration(y=y, sr=sr)
    if duration < 60:
        return None, None

    # 计算RMS能量，帧长1秒， hop 0.5秒
    frame_length = sr
    hop_length = sr // 2
    rms = librosa.feature.rms(y=y, frame_length=frame_length, hop_length=hop_length)[0]
    times = librosa.frames_to_time(np.arange(len(rms)), sr=sr, hop_length=hop_length)

    # 平滑能量曲线（滑动平均）
    smooth_window = 5  # 2.5秒平滑
    kernel = np.ones(smooth_window) / smooth_window
    rms_smooth = np.convolve(rms, kernel, mode='same')

    # 归一化
    rms_norm = (rms_smooth - rms_smooth.min()) / (rms_smooth.max() - rms_smooth.min() + 1e-8)

    # 计算窗口大小对应的帧数
    target_frames = int(target_length / 0.5)
    min_frames = int(min_length / 0.5)
    max_frames = int(max_length / 0.5)

    # 排除前奏尾奏
    start_idx = int(skip_start / 0.5)
    end_idx = len(rms_norm) - int(skip_end / 0.5)
    if end_idx <= start_idx + target_frames:
        start_idx = 0
        end_idx = len(rms_norm)

    # 滑动窗口找平均能量最高的
    best_score = -1
    best_start = start_idx
    best_end = end_idx

    for length in range(min_frames, max_frames + 1, 2):
        for start in range(start_idx, end_idx - length + 1):
            score = rms_norm[start:start+length].mean()
            # 奖励中间高两头低的形状（副歌一般是能量慢慢上去再下来）
            mid = start + length // 2
            shape_score = rms_norm[mid] - (rms_norm[start] + rms_norm[start+length-1]) / 2
            total = score + shape_score * 0.1
            if total > best_score:
                best_score = total
                best_start = start
                best_end = start + length

    # 转换为秒
    start_sec = times[best_start]
    end_sec = times[best_end]

    # 合理性检查
    if end_sec - start_sec < 25 or end_sec > duration:
        return None, None

    return round(start_sec, 1), round(end_sec, 1)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        path = sys.argv[1]
        print(f"正在分析: {path}")
        start, end = detect_chorus_v2(path)
        if start:
            print(f"✅ 高潮段: {start//60:.0f}:{start%60:02.0f} - {end//60:.0f}:{end%60:02.0f} ({end-start:.1f}秒)")
        else:
            print("❌ 未检测到高潮")
    else:
        print("用法: python chorus_detector.py <音频文件>")
