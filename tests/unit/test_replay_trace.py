"""离线回放脚本 tests/unit/test_replay_trace.py。

覆盖：intent→PlaybackEvent 映射、classify 采样装配、reduce 推进、
effects 记录、与旧逻辑实际动作的比对（mismatch 报告）、最新 trace 自动发现。
"""
import json

import pytest

from app.music.playback_state import PlaybackStatus
from scripts.replay_trace import (
    PlaybackEffectType,
    PlaybackEventType,
    _find_latest_trace,
    replay,
)


def _header():
    return {"v": "1.0", "app_commit": "test", "pid": 1, "clock": "perf_counter",
            "start_wall": 0.0, "start_perf": 0.0}


def _intent(method, origin="user", track=None, t=0.0):
    return {"kind": "intent", "method": method, "origin": origin,
            "track_ref": track or {"kind": "local", "name": "a.flac"},
            "old_request_id": 1, "lock_wait_ms": 0.0, "t": t}


def _getp(name, res, t=0.0):
    return {"kind": "mpv", "m": "get_property",
            "args": {"args": [name], "kwargs": {}}, "res": res, "t": t}


def _loadfile(t=0.0):
    return {"kind": "mpv", "m": "loadfile",
            "args": {"args": ["a.flac"], "kwargs": {}},
            "res": None, "file_kind": "local", "t": t}


def _mpv(method, t=0.0):
    return {"kind": "mpv", "m": method, "t": t}


def _write(tmp_path, records):
    p = tmp_path / "trace_test.jsonl"
    lines = [json.dumps(_header(), ensure_ascii=False)]
    lines += [json.dumps(r, ensure_ascii=False) for r in records]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


def _effect_types(result):
    return [e.type for _, e in result["effects"]]


def _event_types(result):
    return [e.type for _, e in result["events"]]


class TestReplayCore:
    def test_play_pause_next_sequence(self, tmp_path):
        """play_song → 采样起播 → pause → next(user)：
        状态机应产出 LOAD_FILE/PAUSE_MPV/LOAD_FILE，代际递增到 2。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("playback-time", 1.5, t=101.0),   # elapsed=0.9 > 0.8 宽限 → LOAD_SUCCESS
            _intent("pause", t=102.0),
            _intent("next", t=103.0),
        ])
        result = replay(path)

        assert _event_types(result) == [
            PlaybackEventType.PLAY_REQUESTED,
            PlaybackEventType.LOAD_SUCCESS,
            PlaybackEventType.PAUSE_REQUESTED,
            PlaybackEventType.PLAY_REQUESTED,
        ]
        assert _effect_types(result) == [
            PlaybackEffectType.LOAD_FILE,
            PlaybackEffectType.PAUSE_MPV,
            PlaybackEffectType.LOAD_FILE,
        ]
        assert result["final_state"].status == PlaybackStatus.LOADING
        assert result["final_state"].epoch == 2
        # PAUSE_MPV 未得到实际 mpv pause 动作 → 应报告不一致
        assert any("pause_mpv" in m for m in result["mismatches"])

    def test_full_consistent_no_mismatch(self, tmp_path):
        """意图 + 采样 + 真实 mpv 动作齐全时：effects 全部兑现，无 mismatch。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("playback-time", 2.0, t=101.0),   # → LOAD_SUCCESS / PLAYING
            _getp("idle-active", False, t=101.5),    # PLAYING 态非 idle → 无事件
            _intent("pause", t=102.0),
            _mpv("pause", t=102.01),
            _intent("resume", t=103.0),
            _mpv("play", t=103.01),
            _intent("clear_queue", t=104.0),
            _mpv("stop", t=104.01),
        ])
        result = replay(path)

        assert _effect_types(result) == [
            PlaybackEffectType.LOAD_FILE,
            PlaybackEffectType.PAUSE_MPV,
            PlaybackEffectType.RESUME_MPV,
            PlaybackEffectType.STOP_MPV,
        ]
        assert result["mismatches"] == []
        assert result["final_state"].status == PlaybackStatus.IDLE

    def test_idle_detected_schedules_next(self, tmp_path):
        """PLAYING 中 idle-active=True → IDLE_DETECTED → SCHEDULE_NEXT_EPISODE，
        由随后的 next 意图兑现。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("playback-time", 2.0, t=101.0),   # → PLAYING
            _getp("idle-active", True, t=110.0),     # 播放自然结束 → IDLE_DETECTED
            _intent("next", origin="auto", t=110.5),
            _loadfile(t=110.6),
        ])
        result = replay(path)

        assert _event_types(result) == [
            PlaybackEventType.PLAY_REQUESTED,
            PlaybackEventType.LOAD_SUCCESS,
            PlaybackEventType.IDLE_DETECTED,
            PlaybackEventType.PLAY_REQUESTED,
        ]
        assert _effect_types(result) == [
            PlaybackEffectType.LOAD_FILE,
            PlaybackEffectType.SCHEDULE_NEXT_EPISODE,
            PlaybackEffectType.LOAD_FILE,
        ]
        assert result["mismatches"] == []

    def test_clear_queue_stops(self, tmp_path):
        """clear_queue → STOP_REQUESTED → STOP_MPV；无实际 stop 时报告不一致。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("playback-time", 2.0, t=101.0),
            _intent("clear_queue", t=102.0),
        ])
        result = replay(path)

        assert _effect_types(result) == [
            PlaybackEffectType.LOAD_FILE,
            PlaybackEffectType.STOP_MPV,
        ]
        assert result["final_state"].status == PlaybackStatus.IDLE
        assert any("stop_mpv" in m for m in result["mismatches"])


class TestReplayLatestTrace:
    def test_find_latest_trace(self, tmp_path):
        (tmp_path / "trace_1_1_0.jsonl").write_text("", encoding="utf-8")
        (tmp_path / "trace_2_2_0.jsonl").write_text("", encoding="utf-8")
        latest = _find_latest_trace(str(tmp_path))
        assert latest.endswith("trace_2_2_0.jsonl")

    def test_replay_skips_header_and_decision(self, tmp_path):
        """header（无 kind）与 decision 行应被跳过，不产生事件。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            {"kind": "decision", "decision": "auto_next_fired",
             "last_idle": False, "now_idle": True, "t": 100.2},
        ])
        result = replay(path)
        assert result["records_parsed"] == 1   # 只算 intent（decision 跳过）
        assert len(result["events"]) == 1
        assert result["final_state"].status == PlaybackStatus.LOADING


class TestReplayP0Fixes:
    def test_match_actual_respects_upper_bound(self, tmp_path):
        """P0：匹配必须有时间上界——实际动作超出容差窗口不得跨事件误配。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=110.0),   # 超出 LOAD_FILE 5s 上界（t=100 → ≤105）
        ])
        result = replay(path)
        # LOAD_FILE(100) 与 loadfile(110) 相差 10s → 不匹配 → 报告不一致
        assert any("load_file" in m for m in result["mismatches"])
        # 界内场景：4s 后到达的 loadfile 正常匹配
        path2 = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=104.0),
        ])
        assert replay(path2)["mismatches"] == []

    def test_actual_consumed_once(self, tmp_path):
        """P0：同一实际动作只能被一个 effect 消费——重复 LOAD_FILE 不得复用同一 loadfile。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("playback-time", 1.5, t=101.0),   # → LOAD_SUCCESS / PLAYING
            _intent("pause", t=102.0),
            _mpv("pause", t=102.01),
            _intent("next", t=103.0),                # 第二次 LOAD_FILE，无新 loadfile
        ])
        result = replay(path)
        # 第一次 LOAD_FILE 消费 loadfile(100.1)；第二次 LOAD_FILE 无可用实际动作 → mismatch
        assert any("load_file" in m for m in result["mismatches"])
        # pause_mpv 已由 pause(102.01) 兑现
        assert not any("pause_mpv" in m for m in result["mismatches"])

    def test_sample_accumulates_full_context(self, tmp_path):
        """P0：采样必须累计——单属性喂三属性分类器会让缺失键默认 0/False，严重误判。

        构造：idle-active=True 先到（宽限内，不判定），playback-time=1.5 后到。
        完整 sample {idle:True, playback_time:1.5} → 过宽限后 idle 优先 → LOAD_FAILED；
        若只传 playback_time（idle 缺失默认 False）会误判 LOAD_SUCCESS。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            _getp("idle-active", True, t=100.5),     # 宽限内（elapsed=0.4<0.8）→ 不判定
            _getp("playback-time", 1.5, t=101.0),    # elapsed=0.9≥0.8，完整 sample → LOAD_FAILED
        ])
        result = replay(path)
        # 累计后：idle=True 且过宽限 → LOAD_FAILED（而非单属性的 LOAD_SUCCESS）
        assert PlaybackEventType.LOAD_FAILED in _event_types(result)
        assert PlaybackEventType.LOAD_SUCCESS not in _event_types(result)
        assert result["final_state"].status == PlaybackStatus.FAILED

    def test_unparsable_get_property_counted_as_dropped(self, tmp_path):
        """P0：无法解析的 args（如 {"name": ...}）计入 dropped 统计，不静默丢弃。"""
        path = _write(tmp_path, [
            _intent("play_song", t=100.0),
            _loadfile(t=100.1),
            {"kind": "mpv", "m": "get_property",
             "args": {"name": "idle-active"}, "res": False, "t": 100.5},
        ])
        result = replay(path)
        assert result["stats"]["samples_dropped"] == 1
        assert result["stats"]["samples_parsed"] == 0
