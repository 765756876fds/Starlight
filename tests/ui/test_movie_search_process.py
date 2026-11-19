"""Real Windows child termination, without real DNS or external requests."""
import sys
import time
from pathlib import Path

import psutil
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from app.movie.search_process import SearchProcessPool

pytestmark = pytest.mark.ui


def wait(predicate, seconds=3):
    end = time.monotonic() + seconds
    while not predicate() and time.monotonic() < end:
        QTest.qWait(10)
    assert predicate()


@pytest.fixture
def owned(tmp_path):
    app = QApplication.instance() or QApplication([])
    script = tmp_path / "child.py"
    script.write_text('''import json,sys,socket,ctypes
p=json.load(sys.stdin)
if p.get("stall"):
 def blocked(*a,**k):
  print("DNS_ENTERED",flush=True)
  ctypes.windll.kernel32.Sleep(60000)
 socket.getaddrinfo=blocked
 socket.getaddrinfo("synthetic.invalid",80)
if p.get("large"): print("x"*5000000)
else: print(json.dumps({"status":"ok","items":[{"query":p.get("query")}]}))
''')
    interpreter = Path(sys.executable).with_name('pythonw.exe')
    pool = SearchProcessPool(command=[str(interpreter), "-B", "-u", str(script)])
    children = []
    yield app, pool, children
    pool.close()
    wait(lambda: not pool.active)
    pool.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert all(not psutil.pid_exists(pid) for pid in children)


def test_two_dns_stalls_are_reaped_before_new_search_starts(owned, record_property):
    app, pool, children = owned
    seen, ticks = [], []
    pool.returned.connect(lambda token, data: seen.append((token, data)))
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(time.monotonic()))
    timer.start(10)
    for token in (1, 2):
        assert pool.start(token, {"stall": True}, 3000)
    wait(lambda: all(s["process"].processId() for s in pool.active.values()))
    children.extend(s["process"].processId() for s in pool.active.values())
    assert not pool.start(3, {}, 1000)
    # Windows is executing an uncooperative getaddrinfo stand-in inside each child.
    wait(lambda: all(b"DNS_ENTERED" in s["data"] for s in pool.active.values()))
    descendants = [child.pid for pid in children for child in psutil.Process(pid).children(recursive=True)]
    children.extend(descendants)
    started = time.monotonic()
    pool.cancel()
    assert len(pool.active) == 2  # Kill requested is not resource release.
    wait(lambda: not pool.active)
    assert all(not psutil.pid_exists(pid) for pid in children)
    assert pool.start(3, {"query": "new"}, 2000)
    wait(lambda: bool(seen))
    assert [s[0] for s in seen] == [3]
    assert pool.peak == 2
    timer.stop()
    record_property("cancel_and_new_result_seconds", time.monotonic() - started)
    record_property("owned_descendants", len(descendants))
    record_property("max_gui_gap_ms", max(b-a for a,b in zip(ticks,ticks[1:]))*1000)
    assert time.monotonic() - started < 2


def test_absolute_deadline_kills_blocked_dns(owned):
    _, pool, children = owned
    result = []
    pool.returned.connect(lambda _, data: result.append(data))
    start = time.monotonic()
    assert pool.start(1, {"stall": True}, 250)
    wait(lambda: pool.active[1]["process"].processId() != 0)
    children.append(pool.active[1]["process"].processId())
    wait(lambda: bool(result))
    assert result[0]["error"] == "Timeout" and time.monotonic()-start < 2


def test_repeated_cancel_does_not_accumulate_processes(owned):
    _, pool, children = owned
    for token in range(12):
        assert pool.start(token, {"stall": True}, 2500)
        wait(lambda: pool.active[token]["process"].processId() != 0)
        children.append(pool.active[token]["process"].processId())
        pool.cancel()
        wait(lambda: not pool.active)
    assert not any(psutil.pid_exists(pid) for pid in children)


def test_output_cap_and_failed_start_are_explicit(owned):
    _, pool, _ = owned
    seen = []
    pool.returned.connect(lambda _, data: seen.append(data))
    assert pool.start(1, {"large": True}, 2000)
    wait(lambda: seen)
    assert seen[0]["error"] == "ResponseTooLarge"
    pool.command = ["Z:/missing-starlight-python.exe"]
    assert pool.start(2, {}, 2000)
    wait(lambda: len(seen) == 2)
    assert seen[1]["error"] == "FailedToStart"


def test_close_reaps_only_owned_children(owned):
    _, pool, children = owned
    assert pool.start(1, {"stall": True}, 2000)
    wait(lambda: pool.active[1]["process"].processId() != 0)
    children.append(pool.active[1]["process"].processId())
    pool.shutdown()
    wait(lambda: not pool.active)
    assert not pool.start(2, {}, 1000)
