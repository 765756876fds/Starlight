"""
歌曲高潮（副歌）检测模块
基于MFCC相似度矩阵 + 能量峰值加权
纯本地，无需大模型
"""
import os
import subprocess
import tempfile
import numpy as np
import librosa
from typing import Tuple, Optional


def _load_audio_via_mpv(audio_path: str, sr: int = 22050) -> Tuple[np.ndarray, int]:
    """用mpv解码音频到numpy数组，避免依赖ffmpeg"""
    # mpv路径
    mpv_path = r"D:\新建文件夹 (2)\mpv.exe"
    # 生成临时wav文件
    tmp = tempfile.NamedTemporaryFile(suffix='.wav', delete=False)
    tmp.close()
    try:
        # 用mpv转成wav
        cmd = [
            mpv_path, audio_path,
            "--no-video",
            f"--o={tmp.name}",
            "--really-quiet",
            "--no-terminal",
        ]
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
        # 读wav
        y, sr = librosa.load(tmp.name, sr=sr, mono=True)
        return y, sr
    finally:
        os.unlink(tmp.name)


def detect_chorus(
    audio_path: str,
    hop_length: int = 512,
    frame_length: int = 2048,
    segment_length: float = 5.0,  # 每个分析片段5秒
    min_chorus_length: float = 30.0,  # 高潮最短30秒
    max_chorus_length: float = 60.0,  # 高潮最长60秒
    skip_start: float = 20.0,  # 跳过开头20秒前奏
    skip_end: float = 15.0,  # 跳过结尾15秒
    energy_weight: float = 0.8,  # 能量权重（最响最嗨为主）
    repeat_weight: float = 0.2,  # 重复权重为辅
) -> Tuple[Optional[float], Optional[float]]:
    """
    检测歌曲高潮（副歌）的起止时间
    
    返回: (start_sec, end_sec)，失败返回 (None, None)
    """
    # 1. 加载音频（用mpv解码）
    y, sr = _load_audio_via_mpv(audio_path)
    duration = librosa.get_duration(y=y, sr=sr)
    
    # 太短的歌不分析
    if duration < 30:
        return None, None
    
    # 2. 计算MFCC特征
    mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=13, hop_length=hop_length)
    mfcc = librosa.util.normalize(mfcc, axis=1)
    
    # 3. 按segment_length分帧，计算每段的平均特征
    frames_per_segment = int(segment_length * sr / hop_length)
    n_segments = mfcc.shape[1] // frames_per_segment
    
    segment_features = []
    for i in range(n_segments):
        start = i * frames_per_segment
        end = min((i+1) * frames_per_segment, mfcc.shape[1])
        segment_features.append(mfcc[:, start:end].mean(axis=1))
    segment_features = np.array(segment_features)
    
    # 4. 计算相似度矩阵
    sim_matrix = np.zeros((n_segments, n_segments))
    for i in range(n_segments):
        for j in range(n_segments):
            # 余弦相似度
            a = segment_features[i]
            b = segment_features[j]
            sim_matrix[i][j] = np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-8)
    
    # 5. 找重复段落：每个片段和多少个其他片段相似
    threshold = 0.85  # 相似度阈值
    repeat_count = np.sum(sim_matrix > threshold, axis=1)
    
    # 6. 计算每个片段的能量（RMS）
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]
    segment_energy = []
    for i in range(n_segments):
        start_frame = i * frames_per_segment
        end_frame = min((i+1) * frames_per_segment, len(rms))
        segment_energy.append(rms[start_frame:end_frame].mean())
    segment_energy = np.array(segment_energy)
    # 归一化能量
    segment_energy = (segment_energy - segment_energy.min()) / (segment_energy.max() - segment_energy.min() + 1e-8)
    
    # 7. 综合评分：能量为主（80%），重复度为辅（20%）
    repeat_score = repeat_count / (repeat_count.max() + 1e-8)
    scores = segment_energy * energy_weight + repeat_score * repeat_weight
    
    # 8. 排除开头和结尾
    min_seg = int(skip_start / segment_length)
    max_seg = n_segments - int(skip_end / segment_length)
    if max_seg <= min_seg:
        min_seg = 0
        max_seg = n_segments
    
    # 9. 找评分最高的连续片段
    best_score = -1
    best_start = 0
    best_end = 0
    
    min_seg_len = int(min_chorus_length / segment_length)
    max_seg_len = int(max_chorus_length / segment_length)
    
    for length in range(min_seg_len, max_seg_len + 1):
        for start in range(min_seg, max_seg - length + 1):
            end = start + length
            score = scores[start:end].mean()
            if score > best_score:
                best_score = score
                best_start = start
                best_end = end
    
    # 10. 转换回秒
    start_sec = best_start * segment_length
    end_sec = best_end * segment_length
    
    # 微调：对齐到最近的节拍
    try:
        tempo, beats = librosa.beat.beat_track(y=y, sr=sr, hop_length=hop_length)
        beat_times = librosa.frames_to_time(beats, sr=sr, hop_length=hop_length)
        # 找最接近的节拍
        start_sec = beat_times[np.argmin(np.abs(beat_times - start_sec))]
        end_sec = beat_times[np.argmin(np.abs(beat_times - end_sec))]
    except:
        pass
    
    # 合理性检查
    if end_sec - start_sec < 15 or end_sec > duration:
        return None, None
    
    return round(start_sec, 1), round(end_sec, 1)


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        path = sys.argv[1]
        print(f"正在分析: {path}")
        start, end = detect_chorus(path)
        if start:
            print(f"✅ 高潮段: {start}s - {end}s (时长{end-start:.1f}s)")
        else:
            print("❌ 未检测到高潮段")
    else:
        print("用法: python chorus_detector.py <音频文件路径>")
