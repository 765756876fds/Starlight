#!/usr/bin/env python3
"""一键回放与分析：扫描 logs/trace 下全部 trace_*.jsonl，逐个调用
replay_trace.replay() 做状态机 vs 旧逻辑的决策比对，输出每份统计与不一致明细。

用法：
    python scripts/analyze_trace.py                  # 默认 logs/trace 全部
    python scripts/analyze_trace.py --trace-dir X   # 指定目录

退出码：0 = 全部一致；1 = 存在不一致（供 CI/脚本判定）。
只读不改：不修改任何 trace 文件与业务代码。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Optional

_PROJ_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from scripts.replay_trace import replay  # noqa: E402

# ANSI 颜色（Windows Terminal / 现代控制台支持；不支持时仅退化为普通文本）
_GREEN = "\033[92m"
_RED = "\033[91m"
_RESET = "\033[0m"

# 意图计数口径：发起播放（play_song/play_online/play_search_result）与切歌（next/prev）
_PLAY_METHODS = ("play_song", "play_online", "play_search_result")
_SWITCH_METHODS = ("next", "prev")


def _count_records(jsonl_path: str) -> tuple[int, int, int]:
    """直接扫描 trace 文件：播放发起数 / 切歌数 / shadow_effects 记录数。"""
    play = switch = shadow = 0
    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = rec.get("kind")
            if kind == "intent":
                m = rec.get("method")
                if m in _PLAY_METHODS:
                    play += 1
                elif m in _SWITCH_METHODS:
                    switch += 1
            elif kind == "decision" and rec.get("decision") == "shadow_effects":
                shadow += 1
    return play, switch, shadow


def analyze_file(jsonl_path: str) -> dict[str, Any]:
    """分析单份 trace：replay 结果 + 记录级统计。"""
    result = replay(jsonl_path)
    play, switch, shadow = _count_records(jsonl_path)
    return {
        "file": jsonl_path,
        "plays": play,
        "switches": switch,
        "shadow_effects": shadow,
        "mismatches": result["mismatches"],
        "final_status": result["final_state"].status.value,
    }


def analyze_dir(trace_dir: str) -> list[dict[str, Any]]:
    if not os.path.isdir(trace_dir):
        sys.exit(f"[analyze] 目录不存在: {trace_dir}")
    files = sorted(glob.glob(os.path.join(trace_dir, "trace_*.jsonl")),
                   key=os.path.getmtime)
    if not files:
        sys.exit(f"[analyze] {trace_dir} 下没有 trace_*.jsonl")
    return [analyze_file(f) for f in files]


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="扫描 trace 并比对状态机与旧逻辑真实行为")
    parser.add_argument("--trace-dir", default=None,
                        help="trace 目录（默认 PROJECT_ROOT/logs/trace）")
    args = parser.parse_args(argv)

    trace_dir = args.trace_dir or os.path.join(_PROJ_ROOT, "logs", "trace")
    reports = analyze_dir(trace_dir)

    print(f"==== 分析 {trace_dir}（{len(reports)} 份 trace） ====")
    total_mismatch = 0
    for r in reports:
        mism = len(r["mismatches"])
        total_mismatch += mism
        print(f"[{os.path.basename(r['file'])}] "
              f"播放 {r['plays']} 首 | 切歌 {r['switches']} 次 | "
              f"shadow_effects {r['shadow_effects']} 条 | "
              f"不一致 {mism} 处 | 最终状态 {r['final_status']}")
        for m in r["mismatches"]:
            print(f"    ✗ {m}")
    print()

    if total_mismatch == 0:
        print(_GREEN + "✅ 所有采集的 Trace 均与旧逻辑一致，可安全切换！" + _RESET)
        return 0
    print(_RED + f"❌ 发现 {total_mismatch} 处不一致，暂不可安全切换（明细见上）。" + _RESET)
    return 1


if __name__ == "__main__":
    sys.exit(main())
