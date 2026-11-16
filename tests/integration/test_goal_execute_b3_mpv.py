"""Native B3 probes. A required explicit MPV path, never skip/fake a decoder."""
import os

import pytest

from tools.goal_execute_b3.probe import SCENARIOS, run_case

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("case", SCENARIOS)
def test_real_goal_execution(case, tmp_path):
    # Whole test context runs in a worker; this launcher stays compatible with
    # an already initialized QApplication without doing SQL on the GUI thread.
    import threading
    answers, failures = [], []
    def run():
        try:
            answers.append(run_case(case, tmp_path / case, os.environ["STARLIGHT_TEST_MPV"]))
        except BaseException as exc:
            failures.append(exc)
    worker = threading.Thread(target=run, name="b3-native-case")
    worker.start()
    worker.join(15)
    assert not worker.is_alive(), "native_test_worker_timeout"
    if failures:
        raise failures[0]
    assert answers[0]["cleanup"] == {"threads_remaining": [], "process_exited": True}
