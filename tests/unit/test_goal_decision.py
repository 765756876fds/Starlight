"""Pure decisions; shared goal, state-dependent plans, no executable side effects."""

from dataclasses import replace

import pytest

from app.music.goal_admission import StateToken
from app.music.goal_decision import Candidate, decide


def state(kind="stopped"):
    return StateToken(
        "owner",
        1,
        1,
        1,
        "song" if kind != "stopped" else "",
        1,
        kind == "paused",
        False,
        (),
        100.0,
        kind,
        1 if kind != "stopped" else None,
    )


LOCAL = Candidate("TRY_COLLECTION", "attemptable")
CHART = Candidate("TRY_CHART", "attemptable")


@pytest.mark.parametrize(
    "kind,movie,candidates,expected",
    [
        ("playing", False, (), "NO_OP"),
        ("playing", True, (), "NO_OP"),
        ("paused", False, (), "RESUME_MUSIC"),
        ("stopped", False, (LOCAL,), "TRY_COLLECTION"),
        ("stopped", False, (CHART,), "TRY_CHART"),
        ("stopped", False, (LOCAL, CHART), "TRY_CHART"),
        ("stopped", False, (replace(LOCAL, explicit_match=True), CHART), "TRY_COLLECTION"),
        (
            "stopped",
            False,
            (replace(LOCAL, explicit_match=True), replace(CHART, explicit_match=True)),
            "TRY_CHART",
        ),
        (
            "stopped",
            False,
            (replace(LOCAL, explicit_match=True, excluded=True), CHART),
            "TRY_CHART",
        ),
        ("unknown", False, (LOCAL, CHART), None),
        ("stopped", True, (LOCAL, CHART), None),
        ("paused", None, (LOCAL, CHART), None),
        ("stopped", False, (), None),
    ],
)
def test_same_goal_varies_only_with_evidence(kind, movie, candidates, expected):
    decision = decide(state(kind), movie, candidates, now=100.0)
    assert decision.plan == expected
    assert decision.reason


def test_stale_and_unproven_sessions_never_claim_completed_goal():
    assert decide(state("playing"), False, (), now=102.0).status == "rejected_stale"
    assert decide(replace(state("playing"), entry=None), False, (), now=100.0).plan is None
    assert decide(replace(state("paused"), stopped=True), False, (), now=100.0).plan is None
    assert decide(state("playing"), False, (), now=100.0).status == "prepared"


def test_unknown_does_not_mean_negative_or_absent():
    result = decide(state(), False, (replace(LOCAL, availability="unknown"),), now=100.0)
    assert result.plan is None and result.status == "needs_verification"
    assert decide(state(), False, (LOCAL,), now=100.0).plan == "TRY_COLLECTION"


@pytest.mark.parametrize("goal", ["LISTEN_MY_COLLECTION", "LISTEN_MUSIC:伤感", "PLAY_SONG"])
def test_extra_requirements_are_not_silently_ignored(goal):
    assert decide(state(), False, (LOCAL, CHART), now=100.0, goal=goal).plan is None
