"""The CI harness must preserve failures and isolate each Qt interpreter."""
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest
import yaml

from scripts.ci import run_ui_smoke as runner


@pytest.mark.parametrize("group", runner.GROUPS)
def test_groups_are_real_explicit_files(group):
    assert (runner.ROOT / runner.GROUPS[group]).is_file()
    assert runner.GROUPS[group].startswith("tests/ui/test_")


@pytest.mark.parametrize("exit_code", [0, 1, 0xC0000005, 0xC0000374])
def test_group_preserves_exit_code_and_creates_error_junit_when_absent(
    tmp_path, monkeypatch, exit_code,
):
    output = tmp_path / "reports"
    process = Mock()
    process.wait.return_value = exit_code

    def start(command, **kwargs):
        assert command[:4] == [runner.sys.executable, "-B", "-X", "faulthandler"]
        assert command[command.index("--group") + 1] == "notifications"
        assert kwargs["env"]["QT_QPA_PLATFORM"] == "offscreen"
        assert kwargs["stderr"] == subprocess.STDOUT
        assert kwargs["cwd"] == runner.ROOT
        if exit_code == 0:
            (output / "junit.xml").write_text('<testsuite tests="15"/>', encoding="utf-8")
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    result = runner.run_group("notifications", output, 5)
    process.wait.assert_called_once_with(timeout=5)
    record = json.loads((output / "result.json").read_text("utf-8"))
    assert result == exit_code and record["returncode"] == exit_code
    assert record["process_returncode"] == exit_code and not record["timed_out"]
    suite = ET.parse(output / "junit.xml").getroot()
    assert (suite.attrib.get("errors") == "1") is (exit_code != 0)


def test_timeout_reaps_child_and_is_an_error_instead_of_a_skip(tmp_path, monkeypatch):
    process = Mock()
    process.wait.side_effect = subprocess.TimeoutExpired("fixture", .01)
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    reap = Mock()
    monkeypatch.setattr(runner, "stop_process_tree", reap)
    output = tmp_path / "reports"
    assert runner.run_group("theme", output, .01) == 124
    reap.assert_called_once_with(process)
    record = json.loads((output / "result.json").read_text("utf-8"))
    assert record["timed_out"] and record["returncode"] == 124
    assert ET.parse(output / "junit.xml").getroot().attrib["errors"] == "1"
    assert not list(ET.parse(output / "junit.xml").iter("skipped"))


def test_success_without_junit_is_not_reported_as_success(tmp_path, monkeypatch):
    process = Mock()
    process.wait.return_value = 0
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(return_value=process))
    output = tmp_path / "reports"
    assert runner.run_group("queue", output, 1) != 0
    record = json.loads((output / "result.json").read_text("utf-8"))
    assert record["returncode"] != 0 and record["process_returncode"] == 0


def test_spawn_failure_still_produces_log_and_error_junit(tmp_path, monkeypatch):
    monkeypatch.setattr(runner.subprocess, "Popen", Mock(side_effect=OSError("fixture launch")))
    output = tmp_path / "reports"
    assert runner.run_group("queue", output, 1) == 125
    assert "fixture launch" in (output / "pytest.log").read_text("utf-8")
    assert ET.parse(output / "junit.xml").getroot().attrib["errors"] == "1"


def test_existing_reports_are_not_overwritten(tmp_path):
    original = tmp_path / "pytest.log"
    original.write_text("first failure", encoding="utf-8")
    with pytest.raises(FileExistsError):
        runner.run_group("theme", tmp_path, 1)
    assert original.read_text("utf-8") == "first failure"


def test_each_group_uses_a_separate_child_run(tmp_path, monkeypatch):
    run = Mock(return_value=0)
    monkeypatch.setattr(runner, "run_group", run)
    assert runner.main(["--output-dir", str(tmp_path / "reports")]) == 0
    assert [c.args[0] for c in run.call_args_list] == list(runner.GROUPS)
    assert len({c.args[1] for c in run.call_args_list}) == len(runner.GROUPS)


def test_first_failure_stops_without_rerunning(tmp_path, monkeypatch):
    run = Mock(return_value=1)
    monkeypatch.setattr(runner, "run_group", run)
    assert runner.main(["--output-dir", str(tmp_path / "reports")]) != 0
    assert run.call_count == 1


@pytest.mark.parametrize("args", [
    ["--group", "not-an-allowlisted-test"],
    ["--timeout", "0"],
    ["--child"],
])
def test_invalid_invocations_are_rejected(tmp_path, args):
    with pytest.raises(SystemExit) as error:
        runner.main(["--output-dir", str(tmp_path / "reports"), *args])
    assert error.value.code == 2


def test_output_inside_checkout_is_rejected():
    with pytest.raises(SystemExit) as error:
        runner.main(["--output-dir", str(runner.ROOT / "temporary-reports")])
    assert error.value.code == 2


def test_windows_reaper_targets_only_the_owned_process(monkeypatch):
    process = Mock(pid=43210)
    process.poll.return_value = 0
    terminate = Mock()
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(runner.subprocess, "run", terminate)
    runner.stop_process_tree(process)
    terminate.assert_called_once_with(
        ["taskkill", "/PID", "43210", "/T", "/F"],
        capture_output=True, timeout=10, check=False,
    )
    process.wait.assert_called_once_with(timeout=10)


def test_reaper_helper_timeout_still_kills_and_waits(monkeypatch):
    process = Mock(pid=43210)
    process.poll.return_value = 0
    monkeypatch.setattr(runner, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(
        runner.subprocess, "run", Mock(side_effect=subprocess.TimeoutExpired("taskkill", 10)),
    )
    runner.stop_process_tree(process)
    process.kill.assert_called_once_with()
    process.wait.assert_called_once_with(timeout=10)


def test_workflow_keeps_ui_separate_and_artifacts_narrow():
    path = runner.ROOT / ".github/workflows/ui-smoke.yml"
    workflow = yaml.load(path.read_text("utf-8"), Loader=yaml.BaseLoader)
    assert set(workflow["jobs"]) == {"ui"}
    job = workflow["jobs"]["ui"]
    assert "needs" not in job and job["runs-on"] == "windows-latest"
    assert job["permissions"] == {"contents": "read"}
    upload = next(s for s in job["steps"] if s.get("uses", "").startswith("actions/upload-artifact@"))
    assert upload["if"] == "always()"
    paths = upload["with"]["path"].splitlines()
    assert paths == [
        "${{ runner.temp }}/starlight-ui-reports/summary.md",
        "${{ runner.temp }}/starlight-ui-reports/*/summary.log",
        "${{ runner.temp }}/starlight-ui-reports/*/junit.xml",
        "${{ runner.temp }}/starlight-ui-reports/*/result.json",
    ]
    summary = next(s for s in job["steps"] if 'report_ui_smoke.py' in s.get('run', ''))
    assert summary['if'] == 'always()'
    assert 'pytest.log' not in upload['with']['path']
    assert workflow["on"]["push"]["paths"] == workflow["on"]["pull_request"]["paths"]
