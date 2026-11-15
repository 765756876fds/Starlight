"""Experiment-only invariants: no model files, production commands or cloud calls."""
import hashlib
import json
from pathlib import Path
import struct
from unittest.mock import Mock

import pytest

from tools.semantic_lab import engine
from tools.semantic_lab.analyze import active_candidate
from tools.semantic_lab.audit import audit
from tools.semantic_lab.corpus import ACTIONS, build
from tools.semantic_lab.embedding import norm
from tools.semantic_lab.gguf_inspect import inspect
from tools.semantic_lab.run import percentile, resource_summary, summarize
from tools.semantic_lab.semantics import gate, prediction, rule_baseline, score, valid


ROOT = Path(__file__).resolve().parents[2]


def test_frozen_corpus_and_development_split_are_reproducible():
    data = ROOT/"tools/semantic_lab/data"
    manifest = json.loads((data/"frozen.sha256.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        assert hashlib.sha256((data/name).read_bytes()).hexdigest() == digest
    cases = json.loads((data/"cases.json").read_text(encoding="utf-8"))
    assert cases == build()
    assert len(cases) == 160
    assert len({r["id"] for r in cases}) == 160
    for category in {r["category"] for r in cases}:
        assert sum(r["category"] == category and r["split"] == "dev" for r in cases) == 4
        assert sum(r["category"] == category and r["split"] == "test" for r in cases) == 12
    assert all(r["gold"]["intent"] in ACTIONS for r in cases)


def test_audit_has_real_paths_handlers_and_keeps_known_gaps():
    result = audit(ROOT)
    assert result["counts"] == {"A": 58, "B": 32, "C": 9, "D": 6, "E": 5}
    for row in result["capabilities"]:
        assert all(row["evidence_test_files_exist"].values())
        if row["source"]:
            assert row["line"] is not None
        if row["status"] == "A":
            assert row["parse_matches"]
            # TTS deliberately bypasses the bus; the source documents that path.
            assert row["registrations"] or row["command"] == "system.stop_tts"
    gaps = {r["example"]: r for r in result["capabilities"] if r["status"] == "C"}
    assert gaps["音量加十"]["parsed"]["intent"] == "system.unknown"
    assert gaps["清空播放队列"]["parsed"]["intent"] == "system.unknown"


@pytest.mark.parametrize("text", ["不要停", "别换歌", "他说停一下", "怎么删除歌单", "“换个榜单”"])
def test_shared_gate_rejects_unsafe_model_proposals_without_executing(text, monkeypatch):
    from app.core.command_bus import CommandBus
    monkeypatch.setattr(CommandBus, "execute", Mock(side_effect=AssertionError("experiment cannot execute")))
    row = build()[0] | {"text": text}
    result = gate(prediction("music.pause"), row)
    assert result["intent"] == "none"
    CommandBus.execute.assert_not_called()


def test_rule_baseline_only_parses_and_never_dispatches(monkeypatch):
    from app.core.command_bus import CommandBus
    monkeypatch.setattr(CommandBus, "execute", Mock(side_effect=AssertionError("no real dispatch")))
    for row in build():
        rule_baseline(row)
    CommandBus.execute.assert_not_called()


@pytest.mark.parametrize("scene,expected", [
    ({"music":"playing","movie":"stopped","focus":"none"}, "music.pause"),
    ({"music":"stopped","movie":"playing","focus":"none"}, "movie.pause"),
    ({"music":"playing","movie":"playing","focus":"none"}, "clarify"),
    ({"music":"playing","movie":"playing","focus":"movie"}, "movie.pause"),
    ({"music":"unknown","movie":"stopped","focus":"none"}, "clarify"),
])
def test_context_gate_uses_supplied_state_not_previous_utterance(scene, expected):
    row = build()[0]
    row = row | {"text":"停一下", "state":row["state"] | scene}
    assert gate(prediction("music.pause"), row)["intent"] == expected


def test_unknown_action_and_wrong_parameter_types_fail_validation():
    assert valid(prediction("music.pause"))
    assert not valid(prediction("os.format_disk"))
    assert not valid(prediction("music.volume_set", {"volume":True}))
    assert not valid(prediction("music.next", {"private_token":"synthetic"}))
    assert gate(prediction("os.format_disk"),build()[0])["intent"] == "none"


def test_exact_scoring_does_not_hide_wrong_slots_or_false_execution():
    row = build()[3]
    assert score(prediction("movie.volume_set", {"volume":50}),row)["full_correct"]
    assert not score(prediction("movie.volume_set", {"volume":40}),row)["full_correct"]
    negative = next(r for r in build() if r["text"] == "不要停")
    assert score(prediction("music.pause"), negative)["false_execution_proposal"]


def test_action_topic_is_not_execution_when_negated_or_reported():
    assert not active_candidate(prediction("music.pause",polarity="negative"))
    assert not active_candidate(prediction("music.next",speech_act="reported"))
    assert not active_candidate(prediction("music.next",speech_act="evaluation"))
    assert active_candidate(prediction("music.next",speech_act="evaluation",implicit_action=True))


def gguf(path):
    def string(value):
        raw=value.encode("utf-8")
        return struct.pack("<Q",len(raw))+raw
    raw = b"GGUF"+struct.pack("<IQQ",3,1,1)
    raw += string("general.architecture")+struct.pack("<I",8)+string("fixture")
    raw += string("weight")+struct.pack("<IQIQ",1,1,0,0)
    raw += b"\0"*((-len(raw))%32)+struct.pack("<f",1)
    path.write_bytes(raw)


def test_gguf_checks_real_tensor_extent_and_rejects_invalid_header(tmp_path):
    path=tmp_path/"tiny.gguf"
    gguf(path)
    result=inspect(path,hash_weights=True)
    assert result["known_tensor_end"] == path.stat().st_size
    assert result["known_tensor_bounds_valid"] and result["parameter_count"] == 1
    path.write_bytes(path.read_bytes()[:-1])
    assert not inspect(path)["known_tensor_bounds_valid"]
    path.write_bytes(b"bad")
    with pytest.raises(ValueError,match="not GGUF"):
        inspect(path)


def test_cosine_normalization_and_percentile_are_not_probabilities():
    assert norm([3,4]) == [.6,.8]
    with pytest.raises(ValueError):
        norm([0,0])
    with pytest.raises(ValueError):
        norm([float("nan")])
    assert percentile([0,10],.95) == 9.5
    assert percentile([],.5) is None
    assert resource_summary([],[])["Dedicated Usage"]["idle_median_bytes"] is None


def test_format_failure_cannot_count_as_correct_semantics():
    row=next(r for r in build() if r["split"]=="test" and r["gold"]["intent"]=="none")
    fallback=prediction("none")
    record={"id":row["id"],"raw":None,"final":fallback,"format_valid":False,"latency_ms":1,
            "raw_score":score(fallback,row),"final_score":score(fallback,row)}
    summary=summarize([record],[row])
    assert summary["raw_full_correct"] == 0
    assert summary["final_full_correct"] == 0


def test_failed_resource_sampler_cannot_leak_owned_server(tmp_path, monkeypatch):
    process=Mock(pid=456,returncode=0)
    process.poll.return_value=None
    monkeypatch.setattr(engine.subprocess,"Popen",lambda *a,**kw:process)
    monkeypatch.setattr(engine,"Resources",Mock(side_effect=OSError("synthetic counter failure")))
    with pytest.raises(OSError,match="synthetic counter failure"):
        with engine.Server("fixture.exe","fixture.gguf",tmp_path/"run"):
            pytest.fail("must not enter a server whose sampler failed")
    process.terminate.assert_called_once()
    process.wait.assert_called_once()
