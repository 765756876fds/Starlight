import json
import os
import site
from pathlib import Path
import subprocess
import sys
import time

import pytest


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows process and filesystem ownership")


def test_recovery_keeps_live_and_unproven_workspaces(tmp_path):
    from app.music.online.isolated_host.lifecycle import WorkspaceLifecycle

    parent, trust = tmp_path / "work", tmp_path / "trust"
    parent.mkdir()
    trust.mkdir()
    live = WorkspaceLifecycle(parent, trust)
    copied = parent / ("sl25-" + "f" * 32)
    copied.mkdir()
    sentinel = copied / "unowned.txt"
    sentinel.write_bytes(b"keep")
    receipt = live.ledger / (copied.name + ".json")
    # Simulate a stale process with the same PID and a different creation time.
    # Its old directory identity still cannot authorize removal of this directory.
    forged = dict(live.record, workspace=copied.name, created=live.record["created"] - 1)
    receipt.write_text(json.dumps(forged), encoding="ascii")
    other = WorkspaceLifecycle(parent, trust)
    try:
        assert live.root.is_dir() and live.receipt.is_file()
        assert copied.name in other.deferred and sentinel.read_bytes() == b"keep"
    finally:
        assert other.close()
        assert live.close()


def test_terminating_exact_owner_closes_child_job_and_reclaims_workspace(tmp_path):
    import psutil
    from app.music.online.isolated_host.lifecycle import WorkspaceLifecycle

    parent, trust = tmp_path / "work", tmp_path / "trust"
    parent.mkdir()
    trust.mkdir()
    source = Path(__file__).resolve().parents[2]
    code = """
import json
import sys
from pathlib import Path
from threading import Event

from app.core.logger import get_logger
output = Path(sys.argv[1])
get_logger(log_dir=str(output / "logs"))
from app.music.online.isolated_host.lifecycle import WorkspaceLifecycle
from app.music.online.isolated_host.win32 import OwnedProcess

workspace = WorkspaceLifecycle(output / "work", output / "trust")
child = OwnedProcess([sys.executable, "-B", "-c", "from threading import Event; Event().wait(45)"])
(workspace.root / "owned.txt").write_text("session data", encoding="ascii")
result = output / "ready.tmp"
result.write_text(json.dumps({"pid": child.pid, "root": workspace.root.name}), encoding="ascii")
result.rename(output / "ready.json")
Event().wait(40)
"""
    env = dict(os.environ, PYTHONPATH=os.pathsep.join([str(source), *site.getsitepackages()]))
    log = tmp_path / "owner.log"
    with log.open("wb") as output:
        owner = subprocess.Popen([sys._base_executable, "-B", "-c", code, str(tmp_path)], cwd=tmp_path,
                                 env=env, creationflags=subprocess.CREATE_NO_WINDOW,
                                 stdout=output, stderr=subprocess.STDOUT)
    try:
        ready = tmp_path / "ready.json"
        deadline = time.monotonic() + 12
        while not ready.exists() and owner.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert ready.is_file(), (owner.poll(), log.read_text(encoding="utf-8", errors="replace"))
        data = json.loads(ready.read_bytes())
        child = psutil.Process(data["pid"])
        assert child.ppid() == owner.pid
        root = parent / data["root"]
        assert (root / "owned.txt").is_file()

        owner.kill()
        assert owner.wait(timeout=5) != 0
        child.wait(timeout=5)
        assert not child.is_running()
        assert root.exists()  # Cold-start recovery owns the durable directory.
        startup = WorkspaceLifecycle(parent, trust)
        try:
            assert data["root"] in startup.recovered
            assert not root.exists()
            assert not (startup.ledger / (data["root"] + ".json")).exists()
        finally:
            assert startup.close()
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=5)
