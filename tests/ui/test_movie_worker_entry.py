"""Source entry, real Windows QProcess pipes, and simulated windowed stdio."""
import json
from pathlib import Path
import sys
import threading

import psutil
import pytest
from PySide6.QtCore import QObject, QProcess, Signal

from app.movie.progressive_search import shared_search_service
from tests.ui.test_movie_progressive_search import cms as cms, wait


pytestmark = [pytest.mark.ui, pytest.mark.skipif(sys.platform != "win32", reason="Windows worker pipes")]
ENTRY = Path(__file__).resolve().parents[2] / "main.py"

# Simulate a windowed PyInstaller entry after the bootloader has selected its
# Python runtime. The OS pipe handles still belong to a real Windows child.
WINDOWED_ENTRY = """
import builtins, runpy, sys
entry, behavior, *args = sys.argv[1:]
sys.argv[:] = [entry, *args]
if '--movie-search-worker' in args:
    sys.frozen = True
    sys.stdin = sys.stdout = sys.stderr = None
    original_import = builtins.__import__
    def checked_import(name, *args, **kwargs):
        if name == 'requests':
            if behavior == 'crash':
                raise SystemExit(23)
            print('third-party import output')
        return original_import(name, *args, **kwargs)
    builtins.__import__ = checked_import

def audit(event, values):
    if event == 'import':
        name = values[0]
        if name == 'app.core.log_redirect':
            raise SystemExit(42)
        if '--movie-search-worker' in args and name.startswith((
                'PySide6', 'app.music', 'app.voice', 'app.ui', 'app.core.config',
                'app.movie.movie_controller', 'app.movie.history', 'app.movie.source_manager')):
            raise SystemExit(43)
    if event == 'sqlite3.connect':
        raise SystemExit(44)

sys.addaudithook(audit)
runpy.run_path(entry, run_name='__main__')
"""


def windowed_command(*args, behavior="ok"):
    return [str(Path(sys.executable).with_name("pythonw.exe")), "-B", "-c", WINDOWED_ENTRY,
            str(ENTRY), behavior, *args]


def test_frozen_service_uses_the_single_executable_entry(cms, monkeypatch):
    with monkeypatch.context() as mp:
        mp.setattr(sys, "frozen", True, raising=False)
        mp.setattr(sys, "executable", r"C:\Program Files\Starlight\Starlight.exe")
        # Keep the real QApplication free of the service created by this test.
        class App(QObject):
            aboutToQuit = Signal()
        app = App()
        mp.setattr("app.movie.progressive_search.QCoreApplication.instance", lambda: app)
        service = shared_search_service()
        assert service is shared_search_service()
        assert service.pool.command == [sys.executable, "--movie-search-worker"]
        assert service.pool.working_directory == Path(sys.executable).parent
    service.close()
    service.deleteLater()


@pytest.mark.parametrize("mode,code", [("normal", 42), ("self-check", None), ("ambiguous", 2)])
def test_entry_modes_are_mutually_exclusive(cms, tmp_path, mode, code):
    report = tmp_path / "check.json"
    args = [] if mode == "normal" else ["--self-check", "--report", str(report)]
    if mode == "ambiguous":
        args += ["--movie-search-worker"]
    child = QProcess()
    command = windowed_command(*args)
    child.start(command[0], command[1:])
    try:
        wait(lambda: child.state() == QProcess.NotRunning, 5)
        assert not bytes(child.readAllStandardOutput())
        if mode == "self-check":
            result = json.loads(report.read_text(encoding="utf8"))
            assert result["checks"]
            assert child.exitCode() == (0 if result["ok"] else 1)
        else:
            assert child.exitCode() == code
            assert not report.exists()
    finally:
        if child.state() != QProcess.NotRunning:
            child.kill()
            assert child.waitForFinished(1000)


def test_windowed_worker_uses_only_the_pipes_and_accepts_one_job(cms):
    service, manager, _ = cms.make(1)
    url = manager.get_enabled_sources()[0]["url"]
    pool = service.pool
    pool.command = windowed_command("--movie-search-worker")
    seen = []
    pool.returned.connect(lambda token, result: seen.append((token, result)))
    payload = dict(mode="search", url=url, keyword="电影", budget=2)
    assert pool.start(1, payload, 4000)
    wait(lambda: len(seen) == 1)
    assert seen[0][1]["status"] == "ok"
    assert seen[0][1]["items"][0]["vod_name"] == "Public test"
    assert len(cms.calls) == 1 and cms.calls[0][1]["wd"] == ["电影"]
    assert pool.start(2, dict(payload, keyword="a" * 65536), 4000)
    wait(lambda: len(seen) == 2)
    assert seen[-1][1]["error"] == "WorkerInput" and len(cms.calls) == 1
    pool.command = windowed_command("--movie-search-worker", behavior="crash")
    assert pool.start(3, payload, 4000)
    wait(lambda: len(seen) == 3)
    assert seen[-1][1]["error"] == "WorkerExit" and len(cms.calls) == 1


def test_windowed_search_cap_cancel_and_absolute_timeout(cms):
    service, _, _ = cms.make(2)
    service.pool.command = windowed_command("--movie-search-worker")
    gate = threading.Event()
    cms.modes.update({path: {"wait": gate} for path in ("/s0", "/s1")})
    serials, observed = [], []
    service.batch.connect(lambda serial, _: serials.append(serial))
    service.pool.returned.connect(lambda token, data: observed.append((token, data)))
    old = service.start("旧词")
    wait(lambda: len(cms.calls) == 2)
    pids = [s["process"].processId() for s in service.pool.active.values()]
    assert len(pids) == 2
    # Windows venv launchers can forward into a base interpreter. Check each
    # owned descendant as well as the QProcess PID when the slots are reaped.
    pids.extend(child.pid for pid in tuple(pids) for child in psutil.Process(pid).children(recursive=True))
    new = service.start("新词")
    assert len(service.pool.active) == 2
    wait(lambda: not any(psutil.pid_exists(pid) for pid in pids))
    gate.set()
    wait(lambda: len(serials) == 2)
    assert old != new and serials == [new, new]
    assert service.pool.peak == 2
    assert any(call[1]["wd"] == ["新词"] for call in cms.calls)
    wait(lambda: not service.pool.active)
    gate.clear()
    url = service.manager.get_enabled_sources()[0]["url"]
    assert service.pool.start("deadline", dict(mode="search", url=url, keyword="等待", budget=5), 1600)
    wait(lambda: len(cms.calls) == 5)
    pid = service.pool.active["deadline"]["process"].processId()
    deadline_pids = [pid, *(child.pid for child in psutil.Process(pid).children(recursive=True))]
    wait(lambda: any(token == "deadline" for token, _ in observed))
    assert observed[-1][1]["error"] == "Timeout"
    assert not any(psutil.pid_exists(owned) for owned in deadline_pids)
