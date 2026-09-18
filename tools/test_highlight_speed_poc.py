"""
Highlight 速度优先方案选型 PoC
测试 3 个轻量方案：
1. pychorus - 专门的 chorus 检测
2. librosa_energy - 能量峰值法
3. librosa_repeat - 自相似性重复段落检测
"""
import os
import sys
import time
import json
import numpy as np

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tools", "ai1_poc_output", "speed_poc")
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_SONGS = [
    {
        "name": "A-K歌之王-陈奕迅",
        "path": r"D:\落雪\音乐文件\K歌之王 - 陈奕迅.flac",
        "baseline_highlight": 141.0,  # All-In-One Rule C 结果
    },
    {
        "name": "B-MYALL-浜崎あゆみ",
        "path": r"D:\落雪\音乐文件\MY ALL - 浜崎あゆみ.flac",
        "baseline_highlight": 258.8,
    },
    {
        "name": "C-爱人错过-告五人",
        "path": r"D:\落雪\音乐文件\爱人错过 - 告五人.flac",
        "baseline_highlight": 252.4,
    },
    {
        "name": "D-Komorebi-MXZ",
        "path": r"D:\落雪\音乐文件\Komorebi(警笛警报版本)完整版混音 - MXZ.flac",
        "baseline_highlight": 18.9,
    },
]


def method_pychorus(song_path):
    """方案 1：pychorus"""
    from pychorus import create_chroma, find_chorus

    t0 = time.time()

    # 先创建 chroma
    chroma, sr = create_chroma(song_path)

    # 计算歌曲时长
    import librosa
    y, _ = librosa.load(song_path, sr=None, duration=1)  # 只加载 1 秒取 sr
    total_duration = librosa.get_duration(path=song_path)

    # 找 chorus
    start_time, end_time = find_chorus(chroma, sr, total_duration, clip_length=15)

    t1 = time.time()

    return {
        'start': start_time,
        'end': end_time,
        'duration': end_time - start_time,
        'time_sec': t1 - t0,
        'total_duration': total_duration,
    }


def method_librosa_energy(song_path):
    """方案 2：librosa 能量峰值法
    找能量最高的 20 秒片段，且在歌曲后半段
    """
    import librosa

    t0 = time.time()

    # 加载音频（降采样到 22050 加速）
    y, sr = librosa.load(song_path, sr=22050)

    # 计算短时能量
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

    total_duration = len(y) / sr
    window_sec = 20  # 高潮片段长度

    # 滑动窗口计算平均能量
    window_frames = int(window_sec * sr / hop_length)
    if window_frames > len(rms):
        window_frames = len(rms) // 2

    energy_windows = []
    for i in range(len(rms) - window_frames):
        avg_energy = np.mean(rms[i:i+window_frames])
        start_sec = i * hop_length / sr
        # 加权：后半段加分
        position_weight = 1.2 if start_sec > total_duration * 0.4 else 0.8
        energy_windows.append((avg_energy * position_weight, start_sec))

    # 找能量最高的窗口
    best = max(energy_windows, key=lambda x: x[0])
    best_start = best[1]

    t1 = time.time()

    return {
        'start': best_start,
        'end': best_start + window_sec,
        'duration': window_sec,
        'time_sec': t1 - t0,
        'total_duration': total_duration,
    }


def method_librosa_repeat(song_path):
    """方案 3：librosa 自相似性 - 找重复段落
    找在歌曲中重复出现最多的段落（通常是 chorus）
    """
    import librosa

    t0 = time.time()

    # 加载音频
    y, sr = librosa.load(song_path, sr=22050)

    # 提取 chroma 特征（音乐内容）
    chroma = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)

    total_duration = len(y) / sr

    # 计算自相似性矩阵
    from scipy.spatial.distance import cdist
    S = librosa.util.sync(chroma, np.arange(0, chroma.shape[1], 10))  # 降采样加速
    D = cdist(S.T, S.T, metric='cosine')

    # 找重复次数最多的段落
    # 简化：找距离最近的匹配对最多的时间段
    n_frames = S.shape[1]
    window_frames = int(20 * 22050 / 512 / 10)  # 20秒窗口

    scores = []
    for i in range(n_frames - window_frames):
        # 这个时间段和其他时间段的相似性
        window = D[i:i+window_frames, :]
        # 找除了自身以外的高相似区域
        mask = np.ones(n_frames, dtype=bool)
        mask[i:i+window_frames] = False
        max_sim = np.max(window[:, mask])
        scores.append((max_sim, i * 5120 / 22050))  # 转换回秒

    # 选重复度最高的，且在后半段
    scores.sort(key=lambda x: x[0], reverse=True)
    # 优先选后半段的
    candidates = [s for s in scores[:20] if s[1] > total_duration * 0.3]
    if candidates:
        best_start = candidates[0][1]
    else:
        best_start = scores[0][1]

    t1 = time.time()

    return {
        'start': best_start,
        'end': best_start + 20,
        'duration': 20,
        'time_sec': t1 - t0,
        'total_duration': total_duration,
    }


def evaluate_quality(result_start, baseline_start, total_duration):
    """简单质量评估"""
    diff = abs(result_start - baseline_start)

    # 偏差小于 15% 总时长 = 明显合理
    if diff < total_duration * 0.15:
        return "A-明显合理"
    # 偏差小于 30% = 基本合理
    elif diff < total_duration * 0.3:
        return "B-基本合理"
    # 偏差小于 50% = 勉强
    elif diff < total_duration * 0.5:
        return "C-勉强"
    # 偏差太大 = 错误
    else:
        return "D-明显错误"


def main():
    print("=" * 70)
    print("Highlight 速度优先方案选型 PoC")
    print("=" * 70)

    methods = [
        ("pychorus", method_pychorus),
        ("librosa_energy", method_librosa_energy),
        ("librosa_repeat", method_librosa_repeat),
    ]

    all_results = {}

    for method_name, method_func in methods:
        print(f"\n{'='*70}")
        print(f"方案: {method_name}")
        print(f"{'='*70}")

        song_results = []

        for song in TEST_SONGS:
            name = song['name']
            path = song['path']
            baseline = song['baseline_highlight']

            print(f"\n  分析: {name}")

            try:
                result = method_func(path)
                quality = evaluate_quality(
                    result['start'], baseline, result.get('total_duration', 220)
                )

                print(f"    位置: {result['start']:.1f}s - {result['end']:.1f}s")
                print(f"    耗时: {result['time_sec']:.2f}s")
                print(f"    Baseline: {baseline}s")
                print(f"    质量: {quality}")

                song_results.append({
                    'song': name,
                    'result': result,
                    'baseline': baseline,
                    'quality': quality,
                })
            except Exception as e:
                print(f"    ❌ 失败: {e}")
                song_results.append({
                    'song': name,
                    'error': str(e),
                    'quality': 'D-明显错误',
                })

        # 统计
        times = [r['result']['time_sec'] for r in song_results if 'result' in r]
        avg_time = np.mean(times) if times else 0
        est_144 = avg_time * 144 / 60  # 分钟

        quality_counts = {"A-明显合理": 0, "B-基本合理": 0, "C-勉强": 0, "D-明显错误": 0}
        for r in song_results:
            quality_counts[r['quality']] += 1

        print(f"\n  --- {method_name} 统计 ---")
        print(f"    平均耗时: {avg_time:.2f}s/首")
        print(f"    144 首预计: {est_144:.1f} 分钟 ({est_144/60:.1f} 小时)")
        print(f"    明显合理: {quality_counts['A-明显合理']}/4")
        print(f"    基本合理: {quality_counts['B-基本合理']}/4")
        print(f"    勉强: {quality_counts['C-勉强']}/4")
        print(f"    明显错误: {quality_counts['D-明显错误']}/4")

        all_results[method_name] = {
            'songs': song_results,
            'avg_time': avg_time,
            'est_144_min': est_144,
            'quality_counts': quality_counts,
        }

    # 保存结果
    with open(os.path.join(OUTPUT_DIR, "speed_poc_summary.json"), 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)

    # 最终对比表
    print("\n" + "=" * 70)
    print("最终对比")
    print("=" * 70)
    print(f"{'方案':<20} {'平均耗时':<12} {'144首预计':<15} {'质量(A/B/C/D)':<20}")
    print("-" * 70)

    for method_name, data in all_results.items():
        qc = data['quality_counts']
        quality_str = f"{qc['A-明显合理']}/{qc['B-基本合理']}/{qc['C-勉强']}/{qc['D-明显错误']}"
        print(f"{method_name:<20} {data['avg_time']:.2f}s/首     {data['est_144_min']:.1f} 分钟        {quality_str}")

    # All-In-One baseline
    print(f"{'All-In-One(baseline)':<20} 208.1s/首    {8.3:.1f} 小时         4/0/0/0")

    print("\n" + "=" * 70)
    print("PoC 完成！")
    print("=" * 70)


if __name__ == "__main__":
    main()
