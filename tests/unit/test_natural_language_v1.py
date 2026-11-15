"""Frozen regression corpus, separate unseen set and legacy execution boundaries."""

import hashlib
import json
from pathlib import Path

import pytest

from app.core.command_bus import CommandType as C
from app.voice.intent import IntentRouter, RuleBasedIntent
from app.voice.natural_language import extract


CORPUS = Path(__file__).parents[1] / "data/natural_voice_v1.json"
CASES = json.loads(CORPUS.read_text(encoding="utf-8"))["cases"]
FINAL = CORPUS.with_name("natural_voice_v1_final.json")
FINAL_CASES = json.loads(FINAL.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_frozen_meanings(case):
    meaning = extract(case["text"])
    assert (meaning.action if meaning else "none") == case["action"], case["text"]


@pytest.mark.parametrize("case", [c for c in CASES if c["action"] == "none"], ids=lambda c: c["id"])
def test_frozen_safety_reaches_actual_router(case):
    intent = IntentRouter({}).parse(case["text"])
    assert intent.intent_type in ("system.unknown", "system.clarify"), (case["text"], intent)


@pytest.mark.parametrize("case", [c for c in CASES if c["action"] != "none"], ids=lambda c: c["id"])
def test_frozen_accepted_meanings_reach_existing_bus_boundary(case):
    intent = IntentRouter({}).parse(case["text"])
    assert intent.intent_type == C.NATURAL_MEDIA
    assert intent.params["action"] == case["action"]


@pytest.mark.parametrize(
    "text",
    [
        "暂停",
        "继续",
        "音乐暂停",
        "电影暂停",
        "下一集",
        "下一首",
        "停止播放",
        "别说了",
        "播放不要说话",
        "播放电影暂停",
        "石头人闪现用了",
    ],
)
def test_explicit_contract_keeps_priority(text):
    before = RuleBasedIntent.match(text)
    after = IntentRouter({}).parse(text)
    assert before is not None
    assert (before.intent_type, before.params) == (after.intent_type, after.params)


def test_corrective_scope_and_playlist_queue_are_distinct():
    intent = IntentRouter({}).parse("不是换歌，是换榜单")
    assert intent.intent_type == C.NATURAL_MEDIA
    assert intent.params["correction"] and intent.params["action"] == "chart"
    assert IntentRouter({}).parse("清空歌单").intent_type == "system.clarify"
    assert IntentRouter({}).parse("清空队列").intent_type == C.MUSIC_CLEAR_QUEUE
    assert IntentRouter({}).parse("下一部电影").intent_type == C.MOVIE_RECOMMEND


def test_frozen_file_is_identifiable():
    assert (
        hashlib.sha256(CORPUS.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == "6d8c8c6b33ff5793781e96d4071efa80ca63bf4141a02f0a630dfb6a0204e6e7"
    )


@pytest.mark.parametrize(
    "case", [c for c in FINAL_CASES if c["action"] == "none"], ids=lambda c: c["id"]
)
def test_final_unseen_rejections_keep_their_execution_boundary(case):
    assert extract(case["text"]) is None
    assert IntentRouter({}).parse(case["text"]).intent_type in ("system.unknown", "system.clarify")


def test_final_file_stays_frozen_including_unrecognized_answers():
    assert (
        hashlib.sha256(FINAL.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
        == "b37fe7b290fdc4ede5b045b83be10972718f026f6edc9ef9e31454a09d3ea592"
    )


@pytest.mark.parametrize("text", ["播放工作歌单", "新建歌单叫工作", "把这首歌加到工作歌单", "把晴天加入工作歌单"])
def test_named_playlist_arguments_do_not_disable_language_safety(text):
    before = RuleBasedIntent.match(text)
    after = IntentRouter({}).parse(text)
    assert before is not None
    assert (after.intent_type, after.params) == (before.intent_type, before.params)
    for prefix in ("不要", "他说", "为什么"):
        assert IntentRouter({}).parse(prefix + text).intent_type in ("system.unknown", "system.clarify")
