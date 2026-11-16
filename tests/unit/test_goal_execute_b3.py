"""No native process required: observer contract and execution admission."""
import math

import pytest

from tools.goal_execute_b3.execution import Executor, _state
from tools.goal_shadow_b2.decision import Outcome


@pytest.mark.parametrize("props,expected", [
    ({"idle-active": True}, ("stopped", False)),
    ({"idle-active": False, "path": "generated", "pause": True, "time-pos": 0}, ("paused", True)),
    ({"idle-active": False, "path": "generated", "pause": False, "time-pos": 0, "time-pos-after": .1}, ("playing", True)),
    ({"idle-active": False, "path": "wrong", "pause": False, "time-pos": 0, "time-pos-after": 1}, ("unknown", False)),
    ({"idle-active": False, "path": "generated", "pause": False, "time-pos": None}, ("unknown", False)),
    ({"idle-active": False, "path": "generated", "pause": False, "time-pos": math.nan}, ("unknown", False)),
    ({"idle-active": False, "path": "generated", "pause": False, "time-pos": 1, "time-pos-after": 1}, ("unknown", False)),
    ({"idle-active": False, "path": "generated", "pause": False, "time-pos": 1, "time-pos-after": math.inf}, ("unknown", False)),
    ({}, ("unknown", False)),
])
def test_decoder_evidence_requires_identity_and_progress(props, expected):
    assert _state(props, {"path": "generated"}) == expected


def test_executor_cannot_bind_an_arbitrary_or_production_controller():
    with pytest.raises(TypeError, match="owned_active_lab_only"):
        Executor(object())


def test_accepted_and_unknown_are_not_started():
    from tools.goal_execute_b3.execution import Result
    result = Result(Outcome.UNKNOWN, True, False, 1, "accepted_but_unobserved")
    assert result.accepted and not result.goal_satisfied
    assert result.status is not Outcome.STARTED and result.user_satisfaction == "unobserved"


def test_currency_rechecked_after_real_state_read_before_confirming():
    """A port reply may arrive after the source/lease changed; no false STARTED."""
    import time
    from types import SimpleNamespace
    lease = [True]
    class Port:
        def sample(self, deadline):
            lease[0] = False
            return {"idle-active": False, "path": "generated", "pause": False,
                    "time-pos": 0, "time-pos-after": 1}
    adapter = object.__new__(Executor)
    adapter.lab = SimpleNamespace(manual_revision=1, ctrl=SimpleNamespace(_play_request_id=2), port=Port())
    status, reason, _ = adapter._confirm("generated", 2, 1, time.monotonic()+.1, lambda: lease[0])
    assert status is Outcome.STALE and reason == "currency_changed_during_read"
