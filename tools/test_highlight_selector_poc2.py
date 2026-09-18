"""
Highlight Selector PoC 2
测试 4 种 Highlight Selector 规则在不同结构歌曲上的表现
"""
import os
import sys
import time
import json
from pathlib import Path

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tools", "ai1_poc_output", "poc2")
os.makedirs(OUTPUT_DIR, exist_ok=True)

TEST_SONGS = [
    {
        "name": "A-K歌之王-陈奕迅",
        "path": r"D:\落雪\音乐文件\K歌之王 - 陈奕迅.flac",
        "category": "A-副歌明显结构标准",
    },
    {
        "name": "B-MYALL-浜崎あゆみ",
        "path": r"D:\落雪\音乐文件\MY ALL - 浜崎あゆみ.flac",
        "category": "B-多个明显chorus",
    },
    {
        "name": "C-爱人错过-告五人",
        "path": r"D:\落雪\音乐文件\爱人错过 - 告五人.flac",
        "category": "C-高潮靠后最终副歌明显",
    },
    {
        "name": "D-Komorebi-MXZ",
        "path": r"D:\落雪\音乐文件\Komorebi(警笛警报版本)完整版混音 - MXZ.flac",
        "category": "D-结构复杂",
    },
]


def get_chorus_segments(segments):
    """提取所有 chorus 段"""
    choruses = []
    for seg in segments:
        label = seg.label.lower() if hasattr(seg, 'label') else seg['label'].lower()
        if 'chorus' in label or 'hook' in label or 'refrain' in label:
            start = seg.start if hasattr(seg, 'start') else seg['start']
            end = seg.end if hasattr(seg, 'end') else seg['end']
            choruses.append({
                'label': label,
                'start': start,
                'end': end,
                'duration': end - start,
            })
    return choruses


def rule_a_longest_chorus(choruses, total_duration):
    """Rule A：最长 chorus"""
    if not choruses:
        return None
    longest = max(choruses, key=lambda x: x['duration'])
    return {
        'rule': 'A-longest',
        'start': longest['start'],
        'end': longest['end'],
        'duration': longest['duration'],
    }


def rule_b_last_chorus(choruses, total_duration):
    """Rule B：最后一个 chorus"""
    if not choruses:
        return None
    last = choruses[-1]
    return {
        'rule': 'B-last',
        'start': last['start'],
        'end': last['end'],
        'duration': last['duration'],
    }


def rule_c_last_long_chorus(choruses, total_duration, min_duration_ratio=0.15):
    """Rule C：最后一个较长 chorus（过滤掉明显过短的）"""
    if not choruses:
        return None
    min_duration = total_duration * min_duration_ratio
    long_choruses = [c for c in choruses if c['duration'] >= min_duration]
    if not long_choruses:
        long_choruses = choruses  # 如果没有长的，就用所有
    last_long = long_choruses[-1]
    return {
        'rule': 'C-last_long',
        'start': last_long['start'],
        'end': last_long['end'],
        'duration': last_long['duration'],
    }


def rule_d_combined_score(choruses, total_duration):
    """Rule D：综合评分"""
    if not choruses:
        return None

    best_score = -1
    best = None

    for i, c in enumerate(choruses):
        # 时长得分（归一化）
        max_dur = max(x['duration'] for x in choruses)
        dur_score = c['duration'] / max_dur if max_dur > 0 else 0.5

        # 位置得分：越靠后分越高（但不是最后最好）
        position_score = (c['start'] + c['end']) / 2 / total_duration

        # 是否在后半段
        second_half_score = 1.0 if (c['start'] + c['end']) / 2 > total_duration / 2 else 0.5

        # 出现次数加权（后面的 chorus 稍微加分）
        order_score = (i + 1) / len(choruses)

        # 综合评分
        score = dur_score * 0.4 + position_score * 0.2 + second_half_score * 0.25 + order_score * 0.15

        if score > best_score:
            best_score = score
            best = c

    return {
        'rule': 'D-combined',
        'start': best['start'],
        'end': best['end'],
        'duration': best['duration'],
        'score': best_score,
    }


def analyze_song(song_info, device='cpu'):
    """分析单首歌曲"""
    import allin1_infer

    name = song_info['name']
    path = song_info['path']

    print(f"\n{'='*60}")
    print(f"分析: {name}")
    print(f"文件: {path}")
    print(f"{'='*60}")

    if not os.path.exists(path):
        print(f"❌ 文件不存在: {path}")
        return None

    out_dir = os.path.join(OUTPUT_DIR, name)
    os.makedirs(out_dir, exist_ok=True)

    print("开始分析...")
    t0 = time.time()

    result = allin1_infer.analyze(
        paths=path,
        out_dir=out_dir,
        visualize=False,
        device=device,
        multiprocess=False,
        overwrite=True,
    )

    t1 = time.time()
    analysis_time = t1 - t0

    print(f"分析完成，耗时: {analysis_time:.1f}s")

    # 获取 segments
    segments = result.segments if hasattr(result, 'segments') else []
    total_duration = segments[-1].end if segments else 0

    print(f"总时长: {total_duration:.1f}s")
    print(f"结构段数: {len(segments)}")

    # 提取 chorus
    choruses = get_chorus_segments(segments)
    print(f"chorus 段数: {len(choruses)}")
    for i, c in enumerate(choruses):
        print(f"  chorus [{i}]: {c['start']:.1f}s - {c['end']:.1f}s ({c['duration']:.1f}s)")

    # 四种规则
    rule_a = rule_a_longest_chorus(choruses, total_duration)
    rule_b = rule_b_last_chorus(choruses, total_duration)
    rule_c = rule_c_last_long_chorus(choruses, total_duration)
    rule_d = rule_d_combined_score(choruses, total_duration)

    print(f"\n--- Highlight Selector 对比 ---")
    if rule_a:
        print(f"Rule A (最长chorus): {rule_a['start']:.1f}s - {rule_a['end']:.1f}s ({rule_a['duration']:.1f}s)")
    else:
        print(f"Rule A (最长chorus): 无 chorus 段")
    if rule_b:
        print(f"Rule B (最后chorus): {rule_b['start']:.1f}s - {rule_b['end']:.1f}s ({rule_b['duration']:.1f}s)")
    else:
        print(f"Rule B (最后chorus): 无 chorus 段")
    if rule_c:
        print(f"Rule C (最后长chorus): {rule_c['start']:.1f}s - {rule_c['end']:.1f}s ({rule_c['duration']:.1f}s)")
    else:
        print(f"Rule C (最后长chorus): 无 chorus 段")
    if rule_d:
        print(f"Rule D (综合评分): {rule_d['start']:.1f}s - {rule_d['end']:.1f}s ({rule_d['duration']:.1f}s, score={rule_d['score']:.3f})")
    else:
        print(f"Rule D (综合评分): 无 chorus 段")

    # 保存结果
    output = {
        'name': name,
        'category': song_info['category'],
        'path': path,
        'analysis_time_sec': analysis_time,
        'total_duration_sec': total_duration,
        'num_segments': len(segments),
        'num_choruses': len(choruses),
        'choruses': choruses,
        'rules': {
            'A': rule_a,
            'B': rule_b,
            'C': rule_c,
            'D': rule_d,
        },
        'all_segments': [
            {
                'label': seg.label if hasattr(seg, 'label') else seg['label'],
                'start': seg.start if hasattr(seg, 'start') else seg['start'],
                'end': seg.end if hasattr(seg, 'end') else seg['end'],
            }
            for seg in segments
        ],
    }

    output_file = os.path.join(OUTPUT_DIR, f"{name}_result.json")
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n结果已保存: {output_file}")
    return output


def main():
    print("=" * 60)
    print("Highlight Selector PoC 2")
    print("=" * 60)
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"设备: CPU (AMD RX 5700 不支持 CUDA)")

    results = []
    t_start = time.time()

    for song in TEST_SONGS:
        result = analyze_song(song)
        if result:
            results.append(result)

    t_end = time.time()
    total_time = t_end - t_start

    # 汇总报告
    print("\n" + "=" * 60)
    print("汇总报告")
    print("=" * 60)
    print(f"总耗时: {total_time:.1f}s")
    print(f"歌曲数: {len(results)}")
    print(f"平均每首: {total_time / len(results):.1f}s")

    print(f"\n--- 4 种规则对比 ---")
    for r in results:
        print(f"\n[{r['category']}] {r['name']}")
        print(f"  时长: {r['total_duration_sec']:.1f}s, chorus数: {r['num_choruses']}")
        for rule_name, rule_result in r['rules'].items():
            if rule_result:
                print(f"  {rule_name}: {rule_result['start']:.1f}s - {rule_result['end']:.1f}s ({rule_result['duration']:.1f}s)")

    # 保存汇总
    summary_file = os.path.join(OUTPUT_DIR, "summary.json")
    with open(summary_file, 'w', encoding='utf-8') as f:
        json.dump({
            'total_time_sec': total_time,
            'num_songs': len(results),
            'avg_time_per_song': total_time / len(results) if results else 0,
            'results': results,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n汇总已保存: {summary_file}")
    print("\nPoC 2 完成！")


if __name__ == "__main__":
    main()
