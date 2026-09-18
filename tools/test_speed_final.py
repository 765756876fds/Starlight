"""
Highlight 速度优先方案 - 最终测试
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
        "baseline_highlight": 141.0,
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

    chroma, y, sr, duration = create_chroma(song_path)
    start_sec = find_chorus(chroma, sr, duration, clip_length=15)

    t1 = time.time()

    return {
        'start': start_sec,
        'end': start_sec + 15,
        'duration': 15,
        'time_sec': t1 - t0,
        'total_duration': duration,
    }


def method_librosa_energy_v2(song_path):
    """方案 2：librosa 能量峰值法 v2
    优化：找能量最高的段落，且排除开头和结尾 20%
    """
    import librosa

    t0 = time.time()

    y, sr = librosa.load(song_path, sr=22050)
    total_duration = len(y) / sr

    # 计算短时能量
    hop_length = 512
    rms = librosa.feature.rms(y=y, hop_length=hop_length)[0]

    window_sec = 20
    window_frames = int(window_sec * sr / hop_length)

    # 滑动窗口
    energy_windows = []
    start_frame = int(total_duration * 0.2 * sr / hop_length)  # 排除开头 20%
    end_frame = len(rms) - int(total_duration * 0.05 * sr / hop_length)  # 排除结尾 5%

    for i in range(start_frame, end_frame - window_frames):
        avg_energy = np.mean(rms[i:i+window_frames])
        start_sec = i * hop_length / sr
        # 加权：越靠后越高（高潮通常在后半段）
        position_ratio = start_sec / total_duration
        position_weight = 0.5 + position_ratio  # 从 0.5 到 1.5
        energy_windows.append((avg_energy * position_weight, start_sec))

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


def evaluate_quality(result_start, baseline_start, total_duration):
    """简单质量评估"""
    diff = abs(result_start - baseline_start)

    if diff < total_duration * 0.1:
        return "A-明显合理"
    elif diff < total_duration * 0.2:
        return "B-基本合理"
    elif diff < total_duration * 0.35:
        return "C-勉强"
    else:
        return "D-明显错误"


def main():
    print("=" * 70)
    print("Highlight 速度优先方案 - 最终测试")
    print("环境: Windows 10, Python 3.11.9, Ryzen 5 5600, 16GB RAM")
    print("=" * 70)

    methods = [
        ("pychorus", method_pychorus),
        ("librosa_energy_v2", method_librosa_energy_v2),
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
                print(f"    偏差: {abs(result['start'] - baseline):.1f}s")
                print(f"    质量: {quality}")

                song_results.append({
                    'song': name,
                    'start': result['start'],
                    'end': result['end'],
                    'time_sec': result['time_sec'],
                    'baseline': baseline,
                    'quality': quality,
                })
            except Exception as e:
                print(f"    ❌ 失败: {e}")
                import traceback
                traceback.print_exc()
                song_results.append({
                    'song': name,
                    'error': str(e),
                    'quality': 'D-明显错误',
                })

        # 统计
        times = [r['time_sec'] for r in song_results if 'time_sec' in r]
        avg_time = np.mean(times) if times else 0
        est_144 = avg_time * 144 / 60  # 分钟

        quality_counts = {"A-明显合理": 0, "B-基本合理": 0, "C-勉强": 0, "D-明显错误": 0}
        for r in song_results:
            quality_counts[r['quality']] += 1

        print(f"\n  --- {method_name} 统计 ---")
        print(f"    平均耗时: {avg_time:.2f}s/首")
        print(f"    144 首预计: {est_144:.1f} 分钟 ({est_144/60:.2f} 小时)")
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
    with open(os.path.join(OUTPUT_DIR, "final_summary.json"), 'w', encoding='utf-8') as f:
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
