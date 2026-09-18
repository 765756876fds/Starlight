"""
All-In-One-Infer 本地音乐高潮检测可行性 PoC

实验脚本，不进入正式业务代码。
"""
import os
import sys
import time
import json

# 确保项目根目录在路径中
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

OUTPUT_DIR = os.path.join(PROJECT_ROOT, "tools", "ai1_poc_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# 测试歌曲
TEST_SONGS = [
    {
        "name": "K歌之王 - 陈奕迅",
        "path": r"D:\落雪\音乐文件\K歌之王 - 陈奕迅.flac",
        "category": "A-结构明显",
    },
    {
        "name": "MY ALL - 浜崎あゆみ",
        "path": r"D:\落雪\音乐文件\MY ALL - 浜崎あゆみ.flac",
        "category": "B-结构中等",
    },
    {
        "name": "Komorebi(警笛警报版本) - MXZ",
        "path": r"D:\落雪\音乐文件\Komorebi(警笛警报版本)完整版混音 - MXZ.flac",
        "category": "C-困难样本",
    },
]


def analyze_song(song_path: str, song_name: str):
    """分析单首歌曲"""
    import allin1_infer

    print(f"\n{'='*60}")
    print(f"分析: {song_name}")
    print(f"文件: {song_path}")
    print(f"{'='*60}")

    # 检查文件是否存在
    if not os.path.exists(song_path):
        print(f"❌ 文件不存在: {song_path}")
        return None

    # 输出目录
    song_dir = os.path.join(OUTPUT_DIR, song_name.replace(" ", "_").replace("/", "_"))
    os.makedirs(song_dir, exist_ok=True)

    # 开始分析
    print("开始分析（CPU 模式，可能需要几分钟）...")
    t0 = time.time()

    try:
        result = allin1_infer.analyze(
            paths=song_path,
            out_dir=song_dir,
            visualize=True,
            device="cpu",
            multiprocess=False,  # Windows 下多进程有问题
        )
        t1 = time.time()

        print(f"✅ 分析完成，耗时: {t1 - t0:.1f}s")

        # 提取结构信息
        segments = result.get("segments", [])
        print(f"\n结构段数: {len(segments)}")

        # 找高潮段（chorus 或 main_chorus）
        chorus_segments = []
        for i, seg in enumerate(segments):
            label = seg.get("label", "")
            start = seg.get("start", 0)
            end = seg.get("end", 0)
            print(f"  [{i}] {label}: {start:.1f}s - {end:.1f}s")

            if "chorus" in label.lower() or "main" in label.lower() or "hook" in label.lower():
                chorus_segments.append({
                    "index": i,
                    "label": label,
                    "start": start,
                    "end": end,
                    "duration": end - start,
                })

        # 找最长的 chorus 作为高潮候选
        highlight = None
        if chorus_segments:
            longest = max(chorus_segments, key=lambda x: x["duration"])
            highlight = longest
            print(f"\n🎯 高潮候选:")
            print(f"   标签: {longest['label']}")
            print(f"   时间: {longest['start']:.1f}s - {longest['end']:.1f}s")
            print(f"   时长: {longest['duration']:.1f}s")

        # 保存结果
        output = {
            "song_name": song_name,
            "analysis_time_sec": t1 - t0,
            "num_segments": len(segments),
            "segments": [{"label": s.get("label", ""), "start": s.get("start", 0), "end": s.get("end", 0)} for s in segments],
            "highlight_candidate": highlight,
        }

        output_file = os.path.join(OUTPUT_DIR, f"{song_name.replace(' ', '_').replace('/', '_')}_result.json")
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"\n结果已保存: {output_file}")
        return output

    except Exception as e:
        t1 = time.time()
        print(f"❌ 分析失败: {e}")
        import traceback
        traceback.print_exc()
        return {
            "song_name": song_name,
            "error": str(e),
            "analysis_time_sec": t1 - t0,
        }


def main():
    print("=" * 60)
    print("All-In-One-Infer 本地音乐高潮检测 PoC")
    print("=" * 60)
    print(f"输出目录: {OUTPUT_DIR}")
    print(f"设备: CPU (AMD RX 5700 不支持 CUDA)")

    results = []
    for song in TEST_SONGS:
        result = analyze_song(song["path"], song["name"])
        if result:
            result["category"] = song["category"]
            results.append(result)

    # 汇总报告
    print("\n" + "=" * 60)
    print("汇总报告")
    print("=" * 60)

    for r in results:
        print(f"\n[{r.get('category', '?')}] {r.get('song_name', '?')}")
        if "error" in r:
            print(f"  ❌ 失败: {r['error']}")
        else:
            print(f"  耗时: {r['analysis_time_sec']:.1f}s")
            print(f"  结构段数: {r['num_segments']}")
            if r.get("highlight_candidate"):
                h = r["highlight_candidate"]
                print(f"  🎯 高潮: {h['label']} @ {h['start']:.1f}s - {h['end']:.1f}s")

    # 保存汇总
    summary_file = os.path.join(OUTPUT_DIR, "summary.json")
    with open(summary_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)

    print(f"\n汇总已保存: {summary_file}")
    print("\nPoC 完成！")


if __name__ == "__main__":
    main()
