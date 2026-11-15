"""Contract tests for the isolated basic semantics experiment, not player tests."""

import hashlib
import json
from pathlib import Path

import pytest
from unittest.mock import Mock

from tools.semantic_light.backends import feature_assist, generate, prototypes
from tools.semantic_light.core import ACTIONS, decide, lexical, semantic, valid
from tools.semantic_light.corpus import MOVIE, MUSIC, PAUSED, build, state
from tools.semantic_light.confirmation import cases as confirmation_cases
from tools.semantic_light.run import record, summarize


def strict(plan):
    return {key: plan[key] for key in ("disposition", "actions", "params", "weak_feedback")}


def test_authored_gold_decisions_match_shared_policy_without_using_text_parser():
    rows = build()
    assert len(rows) == 160
    assert len({row["id"] for row in rows}) == 160
    assert sum(row["split"] == "dev" for row in rows) == 40
    dev = {row["text"] for row in rows if row["split"] == "dev"}
    assert not dev & {row["text"] for row in rows if row["split"] == "test"}
    mismatches = []
    for row in rows:
        assert valid(row["semantic"])
        actual = decide(row["semantic"], row["state"], row["text"])
        if strict(actual) != strict(row["gold"]):
            mismatches.append((row["id"], row["text"], actual, row["gold"]))
    assert not mismatches, mismatches


@pytest.mark.parametrize("text", ["不要暂停音乐", "你是不是暂停了", "他说这首不好听", "“换个榜单”"])
def test_development_safety_framing(text):
    assert decide(lexical(text), MUSIC, text)["disposition"] == "ignore"


def test_development_correction_retains_affirmative_clause_only():
    value = lexical("不是让你换歌，是换个榜单")
    assert value == semantic(["change_list"], target="music", polarity="correction")
    assert decide(value, MUSIC)["params"] == {"change_chart": True}


def test_negative_movie_policy_stops_preserves_and_never_resumes_music():
    plan = decide(semantic(["negative_feedback"], "evaluation", "current_item"), MOVIE)
    assert plan["actions"] == ["movie.stop", "movie.recommend"]
    assert plan["params"]["preserve_progress"] is True
    assert "music.resume" not in plan["actions"]


def test_positive_save_is_separate_from_like_only_and_does_not_touch_storage():
    like = semantic(["positive_feedback"], "evaluation", "music")
    assert decide(like, MUSIC)["actions"] == ["music.recommendation_feedback"]
    save = semantic(["positive_feedback", "save"], target="music")
    plan = decide(save, state(liked_playlist_exists=False))
    assert plan["actions"] == [
        "music.recommendation_feedback",
        "music.create_playlist",
        "music.add_to_playlist",
    ]
    assert plan["params"]["name"] == "我喜欢的音乐"


def test_media_ambiguity_stale_history_and_candidate_require_evidence():
    value = semantic(["pause"])
    assert decide(value, state(movie="playing", focus="none"))["disposition"] == "clarify"
    assert decide(value, state(stale=True))["disposition"] == "clarify"
    assert decide(semantic(["continue_watch"]), MUSIC)["disposition"] == "clarify"
    assert decide(semantic(["select_previous"]), MUSIC)["disposition"] == "clarify"
    assert decide(semantic(["resume"]), PAUSED)["actions"] == ["music.resume"]


def test_frozen_data_when_present_and_first_round_stays_separate():
    root = Path(__file__).resolve().parents[2]
    for name in ("semantic_lab", "semantic_light"):
        folder = root / "tools" / name / "data"
        manifest = json.loads((folder / "frozen.sha256.json").read_text(encoding="utf8"))
        for rel, digest in manifest.items():
            assert hashlib.sha256((folder / rel).read_bytes()).hexdigest() == digest
    rows = build()
    frozen = json.loads((root / "tools/semantic_light/data/cases.json").read_text(encoding="utf8"))
    assert frozen == rows
    development = [r for r in rows if r["split"] == "dev"]
    prototype_text = {r["text"] for r in prototypes(development)}
    assert not prototype_text & {r["text"] for r in rows if r["split"] == "test"}


def test_malformed_predictions_and_multi_action_do_not_execute():
    assert decide({"action": "music.next"}, MUSIC)["disposition"] == "clarify"
    assert decide(semantic(["pause", "change_content"]), MUSIC)["disposition"] == "clarify"
    assert "os.execute" not in ACTIONS


def test_generation_receives_only_text_and_same_schema_never_media_state():
    server = Mock()
    value = semantic(["pause"])
    server.request.return_value = {
        "choices": [{"message": {"content": json.dumps(value)}, "finish_reason": "stop"}]
    }
    actual, detail = generate(server, "先歇会")
    assert actual == value and detail["valid"]
    args, kwargs = server.request.call_args
    assert args[0] == "/v1/chat/completions"
    assert args[1]["messages"][-1] == {"role": "user", "content": "先歇会"}
    assert "music_id" not in json.dumps(args[1])
    assert kwargs["timeout"] == 30
    assert args[1]["max_tokens"] == 96


def test_feature_assist_is_disclosed_text_only_and_does_not_mutate_vector_output():
    raw = semantic(["resume"])
    assisted = feature_assist(raw, "不要暂停音乐")
    assert raw == semantic(["resume"])
    assert assisted["polarity"] == "negative"
    assert assisted["target_hint"] == "music"
    assert decide(assisted, MUSIC)["disposition"] == "ignore"


def test_invalid_format_cannot_score_correct_by_falling_back_to_clarify():
    row = {
        "id": "x",
        "category": "x",
        "split": "test",
        "text": "接着放",
        "state": state(music="stopped"),
        "semantic": semantic(["resume"]),
        "gold": {**decide(semantic(["resume"]), state(music="stopped"))},
        "implicit": False,
    }
    value = record(row, None, 1, error="TimeoutError")
    assert not value["strict_correct"] and not value["action_correct"]
    summary = summarize([value], [row])
    assert summary["failures"] == 1 and summary["format_valid"] == 0
    assert summary["strict_correct"] == 0


def test_parameter_error_is_separate_from_action_error():
    row = {
        "id": "x",
        "category": "x",
        "split": "test",
        "text": "倒回10秒",
        "state": MUSIC,
        "semantic": semantic(["seek_backward"]),
        "gold": {**decide(semantic(["seek_backward"]), MUSIC, "倒回10秒")},
        "implicit": False,
    }
    # A deliberately corrupted gold amount tests score accounting, not product policy.
    row["gold"]["params"] = {"seconds": -20, "mode": "relative"}
    result = record(row, row["semantic"], 1)
    assert result["action_correct"] and not result["strict_correct"] and result["parameter_error"]


def test_confirmation_has_new_wording_same_development_and_independent_gold():
    rows = confirmation_cases()
    original = build()
    development = [r for r in rows if r["split"] == "dev"]
    test = [r for r in rows if r["split"] == "test"]
    assert development == [r for r in original if r["split"] == "dev"]
    assert len(test) == 80
    assert not {r["text"] for r in test} & {r["text"] for r in original}
    for row in test:
        assert strict(decide(row["semantic"], row["state"], row["text"])) == strict(row["gold"])
    folder = Path(__file__).resolve().parents[2] / "tools/semantic_light/confirmation_data"
    manifest = json.loads((folder / "frozen.sha256.json").read_text(encoding="utf8"))
    assert (
        hashlib.sha256((folder / "cases.json").read_bytes()).hexdigest() == manifest["cases.json"]
    )
    assert json.loads((folder / "cases.json").read_text(encoding="utf8")) == rows
