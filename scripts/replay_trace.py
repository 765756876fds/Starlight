#!/usr/bin/env python3
"""离线回放脚本：读取 trace JSONL（RecordingMpv + intent/decision 打点），
将记录转换为 PlaybackEvent 与 classify 采样，喂给 PlaybackState 纯 reducer，
对比"状态机算出的 effects"与"旧逻辑真实动作（mpv 记录）"是否一致。

用法：
    python scripts/replay_trace.py <trace.jsonl>
    python scripts/replay_trace.py --trace-dir logs/trace   # 取该目录最新一份
    python scripts/replay_trace.py                          # 默认 logs/trace 最新

只读不改：本脚本不修改任何业务代码、不写回 trace 文件。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import Any, Optional

# 允许从任意 cwd 运行：scripts/ 的父目录即项目根
_PROJ_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJ_ROOT not in sys.path:
    sys.path.insert(0, _PROJ_ROOT)

from app.music.playback_state import (  # noqa: E402
    PlaybackEffect,
    PlaybackEffectType,
    PlaybackEvent,
    PlaybackEventType,
    PlaybackState,
    classify_status,
    reduce,
)

# 意图方法 → 状态机事件类型（未列出的方法不产生事件）
_INTENT_TO_EVENT = {
    "play_song": PlaybackEventType.PLAY_REQUESTED,
    "play_online": PlaybackEventType.PLAY_REQUESTED,
    "play_search_result": PlaybackEventType.PLAY_REQUESTED,
    "next": PlaybackEventType.PLAY_REQUESTED,   # user 触发 = 中断当前播放，代际递增
    "prev": PlaybackEventType.PLAY_REQUESTED,
    "pause": PlaybackEventType.PAUSE_REQUESTED,
    "resume": PlaybackEventType.RESUME_REQUESTED,
    "play": PlaybackEventType.RESUME_REQUESTED,
    "clear_queue": PlaybackEventType.STOP_REQUESTED,
}

# 状态机 effect → 期望在旧逻辑日志里观察到的实际 mpv 动作
_EXPECTED_ACTUAL = {
    PlaybackEffectType.LOAD_FILE: {"loadfile"},
    PlaybackEffectType.PAUSE_MPV: {"pause"},
    PlaybackEffectType.RESUME_MPV: {"play", "toggle_pause"},
    PlaybackEffectType.STOP_MPV: {"stop", "stop_process"},
    # 调度类（切歌/推进队列）：实际体现为 intent next/prev，不在 mpv 动作里，
    # 由 _match_schedule 用 intent 日志单独核对。
    PlaybackEffectType.SCHEDULE_NEXT_EPISODE: set(),
    PlaybackEffectType.ADVANCE_QUEUE: set(),
}

# 不参与核心状态一致性核对的 effect（UI 通知类）
_IGNORED_EFFECTS = frozenset({PlaybackEffectType.NOTIFY_UI})

# 计入"实际动作"日志的 mpv 方法
_ACTUAL_MARKERS = ("loadfile", "pause", "play", "toggle_pause", "stop", "stop_process")

# effect 与对应实际动作的最大时间容差（秒）：
# LOAD_FILE 需容忍在线解析/HTTP 耗时（可长达数秒），其余 IPC 动作应在数百毫秒内兑现
_EFFECT_TOLERANCE = {
    PlaybackEffectType.LOAD_FILE: 5.0,
    PlaybackEffectType.PAUSE_MPV: 0.5,
    PlaybackEffectType.RESUME_MPV: 0.5,
    PlaybackEffectType.STOP_MPV: 0.5,
    PlaybackEffectType.SCHEDULE_NEXT_EPISODE: 2.0,
    PlaybackEffectType.ADVANCE_QUEUE: 2.0,
}

_SAMPLE_PROPS = ("idle-active", "playback-time", "duration")

# mpv 属性名 → classify_status 采样键名（classify 读下划线命名）
_PROP_TO_SAMPLE = {
    "idle-active": "idle",
    "playback-time": "playback_time",
    "duration": "duration",
}


def _intent_to_event(rec: dict[str, Any]) -> Optional[PlaybackEvent]:
    """intent 记录 → PlaybackEvent（epoch 由 reduce 递增，事件侧无需携带）。"""
    method = rec.get("method")
    etype = _INTENT_TO_EVENT.get(method)
    if etype is None:
        return None
    return PlaybackEvent(
        type=etype,
        track=rec.get("track_ref"),
        origin=rec.get("origin") or "user",
    )


def _sample_from_get_property(rec: dict[str, Any]) -> Optional[dict[str, Any]]:
    """mpv get_property 记录 → classify 采样（idle/playback_time/duration）。"""
    args = rec.get("args") or {}
    alist = args.get("args") if isinstance(args, dict) else (args if isinstance(args, list) else [])
    name = alist[0] if isinstance(alist, list) and alist else None
    key = _PROP_TO_SAMPLE.get(name)
    if key is None:
        return None
    return {key: rec.get("res")}


def _match_actual(effect: PlaybackEffect, t: float,
                  actual_log: list[tuple[float, str]]) -> Optional[str]:
    """在 actual_log 中找与 effect 匹配的实际动作（容差按 effect 类型）。"""
    tolerance = _EFFECT_TOLERANCE.get(effect.type, 0.05)
    for at, method in actual_log:
        if method in _EXPECTED_ACTUAL.get(effect.type, set()) and at >= t - tolerance:
            return method
    return None


def _match_schedule(effect: PlaybackEffect, t: float,
                    intents: list[tuple[float, PlaybackEvent]],
                    tolerance: float = 2.0) -> Optional[str]:
    """调度类 effect（切歌/推进队列）：实际体现为随后出现的 next/prev 意图。"""
    for at, ev in intents:
        if ev.type == PlaybackEventType.PLAY_REQUESTED and at >= t - tolerance:
            return "intent next/prev"
    return None


def replay(jsonl_path: str) -> dict[str, Any]:
    """回放一份 trace JSONL，返回结构化对比结果（不打印，供 CLI/测试共用）。

    返回：
    {
      "file": 路径,
      "records_parsed": 解析的记录条数（不含 header）,
      "events": [(t, PlaybackEvent)],      # 喂给 reduce 的事件（时间序）
      "effects": [(t, PlaybackEffect)],    # reduce 产出的全部 effects
      "actual": [(t, method)],             # 旧逻辑真实动作（mpv 记录）
      "mismatches": [str],                 # 状态机要求但旧逻辑未兑现 / 白名单外不一致
      "final_state": PlaybackState,
    }
    """
    state = PlaybackState()
    pending: list[tuple[float, PlaybackEvent]] = []
    events_log: list[tuple[float, PlaybackEvent]] = []
    effects_log: list[tuple[float, PlaybackEffect]] = []
    actual_log: list[tuple[float, str]] = []
    intents_log: list[tuple[float, PlaybackEvent]] = []
    last_loadfile_t: Optional[float] = None
    last_file_kind = "local"
    records_parsed = 0
    mismatches: list[str] = []

    with open(jsonl_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            # 第一条是 header（无 kind 字段或 kind 不在三类中）→ 跳过
            kind = rec.get("kind")
            if kind not in ("intent", "mpv", "decision"):
                continue
            if kind == "decision":
                # 旧决策点日志：回放不消费，也不计入解析记录
                continue
            records_parsed += 1
            t = float(rec.get("t") or 0.0)

            if kind == "intent":
                ev = _intent_to_event(rec)
                if ev is not None:
                    pending.append((t, ev))
                    intents_log.append((t, ev))

            if kind == "mpv":
                m = rec.get("m")
                if m == "loadfile":
                    last_loadfile_t = t
                    last_file_kind = rec.get("file_kind") or "local"
                    actual_log.append((t, "loadfile"))
                elif m in _ACTUAL_MARKERS:
                    actual_log.append((t, m))
                if m == "get_property":
                    sample = _sample_from_get_property(rec)
                    if sample is not None:
                        elapsed = (t - last_loadfile_t) if last_loadfile_t is not None else 0.0
                        ev = classify_status(state, sample, elapsed, last_file_kind)
                        if ev is not None:
                            pending.append((t, ev))

            # 每条记录后立即消费 pending：保证后续采样分类看到已推进的 state。
            # （若统一在循环外消费，classify 看到的永远是初始 IDLE 态，LOAD_SUCCESS 无法产生。）
            while pending:
                t, ev = pending.pop(0)
                new_state, effects = reduce(state, ev)
                events_log.append((t, ev))
                for eff in effects:
                    effects_log.append((t, eff))
                state = new_state

    # 读完整个 trace 后，在完整日志视野下核对 effects 与旧逻辑实际动作
    # （即时匹配会因 loadfile 等实际记录晚于 intent 记录而误报不一致）
    for t, eff in effects_log:
        if eff.type in _IGNORED_EFFECTS:
            continue
        matched = _match_actual(eff, t, actual_log)
        if matched is None and eff.type in (
            PlaybackEffectType.SCHEDULE_NEXT_EPISODE, PlaybackEffectType.ADVANCE_QUEUE):
            matched = _match_schedule(eff, t, intents_log)
        if matched is None:
            mismatches.append(
                f"t={t:.2f} 状态机要求 {eff.type.value}，但旧逻辑日志未见对应实际动作"
                f"（期望 {sorted(_EXPECTED_ACTUAL.get(eff.type, set())) or 'intent next/prev'}）")

    return {
        "file": jsonl_path,
        "records_parsed": records_parsed,
        "events": events_log,
        "effects": effects_log,
        "actual": actual_log,
        "mismatches": [m for m in mismatches if m is not None],
        "final_state": state,
    }


def _find_latest_trace(trace_dir: Optional[str] = None) -> str:
    """取目录下最新一份 trace_*.jsonl。默认 PROJECT_ROOT/logs/trace。"""
    base = trace_dir or os.path.join(_PROJ_ROOT, "logs", "trace")
    if not os.path.isdir(base):
        sys.exit(f"[replay] 目录不存在: {base}（可用 --trace-dir 指定）")
    files = sorted(glob.glob(os.path.join(base, "trace_*.jsonl")),
                   key=os.path.getmtime)
    if not files:
        sys.exit(f"[replay] {base} 下没有 trace_*.jsonl")
    return files[-1]


def _fmt_effect(eff: PlaybackEffect) -> str:
    payload = dict(eff.payload or {})
    brief = {k: str(v)[:24] for k, v in payload.items()}
    return eff.type.value + (f" {brief}" if brief else "")


def main(argv: Optional[list[str]] = None) -> None:
    parser = argparse.ArgumentParser(description="离线回放 trace，比对状态机与旧逻辑行为")
    parser.add_argument("jsonl", nargs="?", default=None,
                        help="trace JSONL 路径；不传则用 --trace-dir 最新一份")
    parser.add_argument("--trace-dir", default=None,
                        help="trace 目录（默认 PROJECT_ROOT/logs/trace）")
    args = parser.parse_args(argv)

    path = args.jsonl or _find_latest_trace(args.trace_dir)
    result = replay(path)

    print(f"[replay] 文件: {result['file']}")
    print(f"[replay] 解析记录: {result['records_parsed']} 条（不含 header）")
    print(f"[replay] 状态机事件: {len(result['events'])} | effects: {len(result['effects'])}"
          f" | 旧逻辑实际动作: {len(result['actual'])}")
    print()
    print("── 状态机事件 → effects（时间序） ──")
    for t, ev in result["events"]:
        print(f"  t={t:10.2f}  {ev.type.value:22s} origin={ev.origin}")
    for t, eff in result["effects"]:
        mark = "  ✓" if not _mismatch_at(result, t, eff) else "  ✗"
        print(f"  t={t:10.2f}  → effect {_fmt_effect(eff):40s} {mark}")
    print()
    print("── 旧逻辑实际动作（mpv 记录） ──")
    for t, method in result["actual"]:
        print(f"  t={t:10.2f}  {method}")
    print()
    if result["mismatches"]:
        print(f"── 不一致（{len(result['mismatches'])} 处） ──")
        for m in result["mismatches"]:
            print(f"  {m}")
    else:
        print("── 一致：状态机 effects 全部得到旧逻辑实际动作兑现（NOTIFY_UI 白名单除外） ──")
    print(f"── 最终状态: {result['final_state'].status.value}"
          f" | epoch={result['final_state'].epoch}"
          f" | fail_streak={result['final_state'].fail_streak}")


def _mismatch_at(result: dict[str, Any], t: float, eff: PlaybackEffect) -> bool:
    """CLI 展示辅助：该 effect 是否命中 mismatch 列表（同类型且时间接近）。"""
    for m in result["mismatches"]:
        if eff.type.value in m and f"t={t:.2f}" in m:
            return True
    return False


if __name__ == "__main__":
    main()
