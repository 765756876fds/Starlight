"""Download manager acceptance with a real, controllable HTTP peer and generated PCM."""

from contextlib import contextmanager, suppress
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from select import select
from threading import Event, Lock, Thread
from time import monotonic
from unittest.mock import Mock
import errno
import json
import os
import sqlite3
import struct
import subprocess
import sys
import wave

import pytest

from app.music import download_assets
from app.music.downloader import DownloadManager
from app.music.online import download_probe
from app.music.online.models import OnlineTrack


pytestmark = pytest.mark.unit
OPTIONS = {"lyrics": False, "cover": False, "metadata": False}
PAUSE_AFTER = 65536


def audio(amplitude):
    target = BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setparams((2, 2, 44100, 0, "NONE", "not compressed"))
        wav.writeframes(struct.pack("<hhhh", amplitude, 0, -amplitude, 0) * 22050)
    return target.getvalue()


FIRST_AUDIO = audio(100)
SECOND_AUDIO = audio(150)


def song(native="original", title="合成录音"):
    return {"source_name": "fixture", "track_id": native, "title": title, "artist": "Test",
            "album": "Synthetic", "duration_seconds": 1,
            "extra": {"_raw": {"id": native, "source": "fixture"}}}


class Adapter:
    def __init__(self, base):
        self.base = base
        self.calls = []

    def download_tiers(self, track):
        return (("standard", "standard"),)

    def get_download_rendition(self, track, native, *, timeout):
        assert isinstance(track, OnlineTrack)
        assert track.source_name == "fixture" and native == "standard" and timeout > 0
        self.calls.append((track.source_name, str(track.track_id), native))
        return {"url": f"{self.base}/{track.track_id}", "source_quality": "standard"}


def eventually(predicate, timeout=7):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        if value := predicate():
            return value
        Event().wait(.01)
    raise AssertionError("timed out waiting for the observed condition")


def state(manager, key, expected):
    def check():
        task = manager.get_task(key)
        return task if task and task["state"] == expected else None

    return eventually(check)


def record(name, **values):
    destination = os.environ.get("STARLIGHT_BATCH20_LIFECYCLE_EVIDENCE")
    if destination:
        with open(destination, "a", encoding="utf-8") as file:
            file.write(json.dumps(dict(name=name, **values), ensure_ascii=False) + "\n")


class Origin:
    def __init__(self):
        self.requests = []
        self.lock = Lock()
        self.active = 0
        self.peak = 0
        self.hold = set()
        self.mode = {}
        self.status = {}
        self.body = {}
        self.closed = Event()
        self.advance = Event()
        self.release = Event()

    def large_requests(self, path):
        with self.lock:
            return [dict(row) for row in self.requests if row["path"] == path
                    and row["method"] == "GET" and not row["range"].startswith("bytes=0-")]


@contextmanager
def controlled_http():
    origin = Origin()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def reply(self):
            path = self.path
            data = origin.body.get(path, FIRST_AUDIO)
            tag = f'"{sha256(data).hexdigest()}"'
            request = self.headers.get("Range", "")
            validator = self.headers.get("If-Range", "")
            start, end = 0, len(data) - 1
            full = self.command == "GET" and not request
            mode = origin.mode.get(path, "")
            status = origin.status.get(path, 200)
            if mode == "expired_transfer" and not full:
                status = 200
            if request and status == 200 and mode != "unknown_total":
                left, _, right = request.removeprefix("bytes=").partition("-")
                is_prefix = left == "0" and bool(right)
                if is_prefix or mode != "ignore_range" and (not validator or validator == tag):
                    start = int(left)
                    end = min(end, int(right)) if right else end
                    status = 206
                    if start:
                        if mode == "bad_range":
                            start += 1
                        if mode == "range_416":
                            status = 416
            row = dict(path=path, method=self.command, range=request, if_range=validator,
                       status=status, sent=0)
            with origin.lock:
                origin.requests.append(row)
            self.send_response(status)
            self.send_header("Content-Type", "audio/wav")
            self.send_header("ETag", tag)
            self.send_header("Accept-Ranges", "bytes")
            body = data[start:end + 1] if status in (200, 206) else b""
            if mode != "unknown_total":
                self.send_header("Content-Length", str(len(body)))
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(data)}")
            if status == 416:
                self.send_header("Content-Range", f"bytes */{len(data)}")
            self.end_headers()
            if self.command == "HEAD" or not body:
                return
            with origin.lock:
                first_full = full and path in origin.hold
                if first_full:
                    origin.hold.remove(path)
                    origin.active += 1
                    origin.peak = max(origin.peak, origin.active)
            if first_full:
                try:
                    self.wfile.write(body[:PAUSE_AFTER])
                    self.wfile.flush()
                    with origin.lock:
                        row["sent"] = PAUSE_AFTER
                    start = monotonic()
                    deadline = start + 12
                    sent = PAUSE_AFTER
                    while not origin.release.is_set() and monotonic() < deadline:
                        if origin.advance.is_set() and sent == PAUSE_AFTER and monotonic() - start > 1.1:
                            sent += 8192
                            self.wfile.write(body[PAUSE_AFTER:sent])
                            self.wfile.flush()
                            with origin.lock:
                                row["sent"] = sent
                        readable, _, _ = select([self.connection], [], [], .025)
                        if readable and not self.connection.recv(1):
                            origin.closed.set()
                            return
                    with suppress(ConnectionError, OSError):
                        self.wfile.write(body[sent:])
                        with origin.lock:
                            row["sent"] = len(body)
                except (ConnectionError, OSError):
                    origin.closed.set()
                finally:
                    with origin.lock:
                        origin.active -= 1
                return
            with suppress(ConnectionError, OSError):
                if mode == "truncated" and (full or not request.startswith("bytes=0-")):
                    body = body[:len(body) // 2]
                self.wfile.write(body)
                with origin.lock:
                    row["sent"] = len(body)

        do_GET = reply
        do_HEAD = reply

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": .02}, daemon=True)
    thread.start()
    try:
        origin.address = f"http://127.0.0.1:{server.server_port}"
        yield origin, origin.address
    finally:
        origin.release.set()
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.fixture
def local_http(monkeypatch):
    monkeypatch.setattr(download_probe, "_is_allowed_ip", lambda ip: ip == "127.0.0.1")
    with controlled_http() as service:
        yield service


def finish(manager):
    manager.close()
    manager._pool.shutdown(wait=True)
    manager._probe_pool.shutdown(wait=True)


@pytest.fixture
def factory(tmp_path, local_http):
    origin, address = local_http
    managers = []

    def make(max_workers=2, directory=None, journal=None):
        manager = DownloadManager(
            Adapter(address), Mock(), str(directory or tmp_path / "media"), max_workers=max_workers,
            journal_path=journal or tmp_path / "download-tasks.sqlite",
        )
        managers.append(manager)
        return manager

    try:
        yield make, origin
    finally:
        for manager in reversed(managers):
            finish(manager)


@pytest.mark.parametrize("mode", ["resume", "ignore_range", "changed", "range_416"])
def test_real_pause_then_valid_resume_or_safe_restart(factory, mode):
    make, origin = factory
    manager = make(max_workers=1)
    origin.hold.add("/original")
    target = song()
    key = manager.task_key(target, OPTIONS)
    assert manager.download(target, options=OPTIONS)
    assert not manager.download(target, options=OPTIONS)
    first = eventually(lambda: (t if (t := manager.get_task(key))["bytes_written"] >= PAUSE_AFTER else None))
    assert first["bytes_written"] == PAUSE_AFTER
    assert first["total_bytes"] == len(FIRST_AUDIO)
    paused_at = monotonic()
    assert manager.pause(key)
    paused = state(manager, key, "paused")
    assert origin.closed.wait(3), "the HTTP peer still owns the connection"
    pause_closed_ms = round((monotonic() - paused_at) * 1000, 1)
    assert paused["bytes_written"] == PAUSE_AFTER and paused["total_bytes"] == len(FIRST_AUDIO)
    assert paused["speed_bps"] == 0
    assert paused["eta_seconds"] is None
    assert not list(Path(manager.download_dir).glob("*.wav"))

    origin.mode["/original"] = mode
    expected = FIRST_AUDIO
    if mode == "changed":
        expected = SECOND_AUDIO
        origin.body["/original"] = SECOND_AUDIO
    assert manager.resume(key)
    completed = state(manager, key, "completed")
    assert completed["generation"] > paused["generation"]
    assert completed["bytes_written"] == completed["total_bytes"] == len(expected)
    path = Path(completed["path"])
    actual_hash = sha256(path.read_bytes()).hexdigest()
    assert actual_hash == sha256(expected).hexdigest()
    assert download_assets.inspect_audio(path)["evidence"] == "decoded"
    attempts = origin.large_requests("/original")
    ranged = next(row for row in attempts if row["range"] == f"bytes={PAUSE_AFTER}-")
    assert ranged["if_range"] == f'"{sha256(FIRST_AUDIO).hexdigest()}"'
    assert ranged["status"] == {"resume": 206, "ignore_range": 200, "changed": 200, "range_416": 416}[mode]
    eventual_bytes = sum(row["sent"] for row in attempts)
    expected_bytes = len(expected) if mode == "resume" else len(expected) + PAUSE_AFTER
    assert eventual_bytes == expected_bytes
    record(mode, requests=attempts, network_body_bytes=eventual_bytes, sha256=actual_hash,
           paused_bytes=paused["bytes_written"], pause_closed_ms=pause_closed_ms)


def test_waiting_cancel_same_name_versions_and_remove(factory):
    make, origin = factory
    manager = make(max_workers=1)
    origin.hold.add("/original")
    studio, live, cancelled = song(), song("live"), song("unused", "取消排队")
    a, b, c = (manager.task_key(item, OPTIONS) for item in (studio, live, cancelled))
    assert len({a, b, c}) == 3
    assert manager.download(studio, options=OPTIONS)
    eventually(lambda: origin.active == 1)
    assert manager.download(live, options=OPTIONS)
    assert manager.download(cancelled, options=OPTIONS)
    assert manager.get_task(b)["state"] == "queued"
    assert manager.cancel(c)
    state(manager, c, "cancelled")
    assert manager.remove(c)
    eventually(lambda: all(t["key"] != c for t in manager.tasks()))
    origin.release.set()
    one, two = state(manager, a, "completed"), state(manager, b, "completed")
    assert one["path"] != two["path"]
    assert Path(one["path"]).read_bytes() == Path(two["path"]).read_bytes() == FIRST_AUDIO
    assert not any(req["path"] == "/unused" for req in origin.requests)
    assert manager.remove(a)
    eventually(lambda: manager.get_task(a) is None)
    assert Path(one["path"]).read_bytes() == FIRST_AUDIO
    record("batch", saved_names=[Path(row["path"]).name for row in (one, two)], requests=origin.requests)


def test_remove_during_media_validation_ignores_late_worker(factory, monkeypatch):
    make, origin = factory
    manager = make(max_workers=1)
    reached, release = Event(), Event()
    original = manager._inspect_audio

    def validate(path):
        reached.set()
        assert release.wait(5)
        return original(path)

    monkeypatch.setattr(manager, "_inspect_audio", validate)
    downloads = []
    target = song()
    key = manager.task_key(target, OPTIONS)
    try:
        assert manager.download(target, {"on_done": lambda *args: downloads.append(args)}, options=OPTIONS)
        assert reached.wait(5)
        assert manager.remove(key)
        assert manager.active_count() == 1
        assert not manager.download(target, options=OPTIONS)
    finally:
        release.set()
    eventually(lambda: manager.active_count() == 0 and manager.get_task(key) is None)
    assert downloads == []
    assert not list(Path(manager.download_dir).glob("*.wav"))
    assert not list(Path(manager.download_dir).rglob("*.part"))
    assert manager.download(target, options=OPTIONS)
    state(manager, key, "completed")


def test_commit_cancel_linearization_repeated_20_times(factory, monkeypatch):
    make, _ = factory
    manager = make(max_workers=1)
    original = download_assets.publish_new
    outcomes = []

    for index in range(20):
        reached, release, began = Event(), Event(), Event()
        cancel_result = []

        def publish(part, target):
            reached.set()
            assert release.wait(5)
            return original(part, target)

        monkeypatch.setattr(download_assets, "publish_new", publish)
        target = song(f"race-{index}", f"发布竞态 {index}")
        key = manager.task_key(target, OPTIONS)
        assert manager.download(target, options=OPTIONS)
        assert reached.wait(6)

        def cancel():
            began.set()
            cancel_result.append(manager.cancel(key))

        thread = Thread(target=cancel, daemon=True)
        try:
            thread.start()
            assert began.wait(2)
        finally:
            release.set()
        thread.join(7)
        assert not thread.is_alive() and len(cancel_result) == 1
        final = state(manager, key, "cancelled" if cancel_result[0] else "completed")
        path = Path(manager.download_dir) / (manager._filename_base(target["title"], target["artist"]) + ".wav")
        assert path.is_file() is (not cancel_result[0])
        if path.is_file():
            assert path.read_bytes() == FIRST_AUDIO
        outcomes.append(dict(accepted=cancel_result[0], state=final["state"], published=path.is_file()))
    record("commit_cancel_20", outcomes=outcomes)


def test_failed_retry_preserves_existing_file_and_protects_journal_secrets(factory, tmp_path):
    make, origin = factory
    manager = make()
    origin.status["/retry"] = 500
    target = song("retry")
    target["extra"]["_raw"].update(url="https://media.invalid/private?token=no-persist",
                                    Authorization="Bearer no-persist", Cookie="no-persist=1")
    key = manager.task_key(target, OPTIONS)
    assert manager.download(target, options=OPTIONS)
    failed = state(manager, key, "failed")
    assert failed["message"]
    assert not list(Path(manager.download_dir).glob("*.wav"))
    origin.status["/retry"] = 200
    assert manager.retry(key)
    completed = state(manager, key, "completed")
    assert completed["generation"] > failed["generation"]
    assert Path(completed["path"]).read_bytes() == FIRST_AUDIO
    finish(manager)
    with sqlite3.connect(tmp_path / "download-tasks.sqlite") as db:
        row = db.execute("SELECT payload FROM downloads WHERE key=?", (key,)).fetchone()[0]
        assert "no-persist" not in row and "Authorization" not in row and "Cookie" not in row
        db.execute("INSERT INTO downloads VALUES (?,?)", ("corrupt", '{"schema":'))
    restored = make()
    recovered = state(restored, key, "completed")
    assert recovered["path"] == completed["path"]
    assert restored.get_task("corrupt") is None


def test_file_publication_error_preserves_external_target(factory, monkeypatch):
    make, _ = factory
    manager = make()

    def fail(part, target):
        raise OSError(errno.ENOSPC, "controlled write error")

    monkeypatch.setattr(download_assets, "publish_new", fail)
    directory = Path(manager.download_dir)
    directory.mkdir()
    protected = directory / "existing.txt"
    protected.write_bytes(b"existing user bytes")
    target = song("disk", "CON.a")
    key = manager.task_key(target, OPTIONS)
    assert manager.download(target, options=OPTIONS)
    state(manager, key, "failed")
    assert protected.read_bytes() == b"existing user bytes"
    assert not list(directory.glob("*.wav"))
    assert manager._filename_base("CON.a", "").startswith("_")
    assert not list(directory.rglob("*.part"))


def test_corrupt_sqlite_isolated(factory, tmp_path):
    make, _ = factory
    journal = tmp_path / "broken.sqlite"
    journal.write_bytes(b"invalid sqlite bytes")
    manager = make(journal=journal)
    assert manager.tasks() == []
    assert journal.read_bytes() == b"invalid sqlite bytes"


def test_abrupt_process_exit_restores_exact_persisted_part(factory, tmp_path):
    make, origin = factory
    origin.hold.add("/original")
    origin.advance.set()
    source = Path(__file__).resolve()
    # Import this fixture module only, then start the real manager in an
    # independent interpreter. Kill only this child, while its GET is blocked.
    child_code = """
import runpy
import sys
from threading import Event
from unittest.mock import Mock
from app.music.downloader import DownloadManager
from app.music.online import download_probe

test = runpy.run_path(sys.argv[1])
download_probe._is_allowed_ip = lambda ip: ip == "127.0.0.1"
manager = DownloadManager(test["Adapter"](sys.argv[2]), Mock(), sys.argv[3], journal_path=sys.argv[4])
assert manager.download(test["song"](), options=test["OPTIONS"])
Event().wait(20)
"""
    journal = tmp_path / "download-tasks.sqlite"
    media = tmp_path / "media"
    # The loopback origin address is passed explicitly by the factory fixture.
    # Do not rediscover it through application or user settings.
    cmd = [sys.executable, "-B", "-c", child_code, str(source), origin.address, str(media), str(journal)]
    child = subprocess.Popen(cmd, cwd=source.parents[2], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    key = DownloadManager.task_key(song(), OPTIONS)

    def durable():
        if child.poll() is not None:
            raise AssertionError(f"isolated child exited early: {child.returncode}")
        if not journal.exists():
            return None
        try:
            with sqlite3.connect(journal, timeout=.05) as db:
                row = db.execute("SELECT payload FROM downloads WHERE key=?", (key,)).fetchone()
                if row:
                    task = json.loads(row[0])
                    return task if task["bytes_written"] >= PAUSE_AFTER + 8192 else None
        except sqlite3.OperationalError:
            return None

    try:
        before = eventually(durable, timeout=10)
        assert origin.active == 1 and not origin.closed.is_set()
        assert before["bytes_written"] == PAUSE_AFTER + 8192
    finally:
        if child.poll() is None:
            child.kill()
        stdout, stderr = child.communicate(timeout=5)
    assert child.returncode != 0, (stdout, stderr)
    assert origin.closed.wait(3), "the child socket remained active after process termination"
    recovered = make()
    paused = state(recovered, key, "paused")
    assert paused["bytes_written"] == before["bytes_written"]
    assert paused["total_bytes"] == len(FIRST_AUDIO)
    assert recovered.resume(key)
    completed = state(recovered, key, "completed")
    assert completed["generation"] > paused["generation"]
    actual = Path(completed["path"]).read_bytes()
    assert actual == FIRST_AUDIO
    sequence = origin.large_requests("/original")
    assert sequence[-1]["range"] == f"bytes={before['bytes_written']}-"
    assert sequence[-1]["status"] == 206
    record("abrupt_exit", returncode=child.returncode, before_bytes=before["bytes_written"],
           recovered_generation=completed["generation"], requests=sequence,
           final_sha256=sha256(actual).hexdigest())


def test_bad_content_range_cannot_append(factory):
    make, origin = factory
    manager = make()
    origin.hold.add("/original")
    target = song()
    key = manager.task_key(target, OPTIONS)
    assert manager.download(target, options=OPTIONS)
    eventually(lambda: manager.get_task(key)["bytes_written"] >= PAUSE_AFTER)
    assert manager.pause(key)
    state(manager, key, "paused")
    assert origin.closed.wait(3)
    origin.mode["/original"] = "bad_range"
    assert manager.resume(key)
    failed = state(manager, key, "failed")
    assert failed["message"]
    assert not list(Path(manager.download_dir).glob("*.wav"))
    sequence = origin.large_requests("/original")
    assert sequence[-1]["range"] == f"bytes={PAUSE_AFTER}-"
    assert sequence[-1]["status"] == 206
    record("bad_range", requests=sequence, message=failed["message"])


def test_truncated_and_corrupt_audio_do_not_block_batch(factory):
    make, origin = factory
    origin.mode["/truncated"] = "truncated"
    origin.body["/corrupt"] = b"not audio" * 25000
    manager = make(max_workers=2)
    songs = (song("truncated", "中断"), song("corrupt", "损坏"), song("good", "正常"))
    for target in songs:
        assert manager.download(target, options=OPTIONS)
    for bad in songs[:2]:
        failed = state(manager, manager.task_key(bad, OPTIONS), "failed")
        assert not failed["path"]
        assert failed["message"]
    good = state(manager, manager.task_key(songs[2], OPTIONS), "completed")
    assert Path(good["path"]).read_bytes() == FIRST_AUDIO
    assert len(list(Path(manager.download_dir).glob("*.wav"))) == 1


@pytest.mark.parametrize("status", [403, 404])
def test_expired_transfer_refreshes_only_same_recording(factory, monkeypatch, status):
    make, origin = factory
    manager = make()
    origin.status["/expired"] = status
    actual = Adapter.get_download_rendition

    def expired_once(adapter, track, native, *, timeout):
        result = actual(adapter, track, native, timeout=timeout)
        if len(adapter.calls) == 1:
            result["url"] = result["url"].replace("/original", "/expired")
        return result

    # For this fixture, only the first full GET expires. Keep HEAD and prefix
    # valid so that the real selection path reaches transfer and refreshes.
    origin.mode["/expired"] = "expired_transfer"
    monkeypatch.setattr(Adapter, "get_download_rendition", expired_once)
    target = song()
    assert manager.download(target, options=OPTIONS)
    completed = state(manager, manager.task_key(target, OPTIONS), "completed")
    assert Path(completed["path"]).read_bytes() == FIRST_AUDIO
    assert manager.online_manager.calls == [("fixture", "original", "standard")] * 2
    sequence = origin.large_requests("/expired") + origin.large_requests("/original")
    assert [req["status"] for req in sequence] == [status, 200]
    record("expired_transfer", requests=sequence, resolver_calls=manager.online_manager.calls)


def test_cancel_wins_before_publication_twenty_times(factory, monkeypatch):
    make, _ = factory
    manager = make(max_workers=1)
    original = manager._inspect_audio
    outcomes = []
    for index in range(20):
        reached, release = Event(), Event()

        def validate(path):
            result = original(path)
            reached.set()
            assert release.wait(5)
            return result

        monkeypatch.setattr(manager, "_inspect_audio", validate)
        target = song(f"cancel-{index}", f"校验竞态 {index}")
        key = manager.task_key(target, OPTIONS)
        try:
            assert manager.download(target, options=OPTIONS)
            assert reached.wait(6)
            assert manager.cancel(key)
            assert manager.active_count() == 1
        finally:
            release.set()
        final = state(manager, key, "cancelled")
        assert not final["path"]
        outcomes.append(dict(accepted=True, state=final["state"], published=False))
    assert not list(Path(manager.download_dir).glob("*.wav"))
    assert not list(Path(manager.download_dir).glob("*.part"))
    record("validation_cancel_20", outcomes=outcomes)


def test_same_file_id_mutation_after_validation_refuses_publication(factory, monkeypatch):
    make, _ = factory
    manager = make()
    reached, release = Event(), Event()
    part_paths = []
    original = manager._inspect_audio

    def validate(path):
        part_paths.append(Path(path))
        return original(path)

    def lyric(_manager, _track):
        reached.set()
        assert release.wait(5)
        return None

    monkeypatch.setattr(manager, "_inspect_audio", validate)
    monkeypatch.setattr(download_assets, "lyric_for_track", lyric)
    target = song()
    key = manager.task_key(target, OPTIONS)
    try:
        assert manager.download(target, options={**OPTIONS, "lyrics": True})
        assert reached.wait(6)
        part = part_paths[0]
        before = part.stat()
        with part.open("r+b") as file:
            file.write(b"not a valid WAV")
        os.utime(part, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
        after = part.stat()
        assert (before.st_dev, before.st_ino, before.st_size) == (after.st_dev, after.st_ino, after.st_size)
        assert before.st_mtime_ns != after.st_mtime_ns
    finally:
        release.set()
    row = state(manager, key, "failed")
    assert not row["path"] and not list(Path(manager.download_dir).glob("*.wav"))
    assert not list(Path(manager.download_dir).glob("*.part"))


def test_acknowledged_cancel_not_resurrected_by_abrupt_exit(factory, tmp_path):
    make, origin = factory
    source = Path(__file__).resolve()
    journal = tmp_path / "download-tasks.sqlite"
    media = tmp_path / "media"
    ready = tmp_path / "validating"
    cancelled = tmp_path / "cancel-accepted"
    child_code = """
import runpy
import sys
from pathlib import Path
from threading import Event
from unittest.mock import Mock
from app.music.downloader import DownloadManager
from app.music.online import download_probe

test = runpy.run_path(sys.argv[1])
download_probe._is_allowed_ip = lambda ip: ip == "127.0.0.1"
manager = DownloadManager(test["Adapter"](sys.argv[2]), Mock(), sys.argv[3], journal_path=sys.argv[4])
active = Event()
def inspect(path):
    active.set()
    Event().wait(20)
    return None
manager._inspect_audio = inspect
song = test["song"]()
key = manager.task_key(song, test["OPTIONS"])
assert manager.download(song, options=test["OPTIONS"])
assert active.wait(8)
Path(sys.argv[5]).write_text("ready", encoding="utf-8")
assert sys.stdin.readline().strip() == "cancel"
assert manager.cancel(key)
assert manager.active_count() == 1
Path(sys.argv[6]).write_text("accepted", encoding="utf-8")
Event().wait(20)
"""
    cmd = [sys.executable, "-B", "-c", child_code, str(source), origin.address, str(media),
           str(journal), str(ready), str(cancelled)]
    child = subprocess.Popen(cmd, cwd=source.parents[2], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        eventually(ready.is_file, timeout=10)
        child.stdin.write(b"cancel\n")
        child.stdin.flush()
        eventually(cancelled.is_file, timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
        stdout, stderr = child.communicate(timeout=5)
    assert child.returncode != 0, (stdout, stderr)
    restored = make()
    key = restored.task_key(song(), OPTIONS)
    task = state(restored, key, "cancelled")
    assert not task["path"]
    assert not restored.resume(key)
    assert not list(media.glob("*.wav"))
    record("cancel_abrupt_exit", returncode=child.returncode,
           state=task["state"], partial_files=len(list(media.glob("*.part"))))


@pytest.mark.parametrize("known_size", [True, False])
def test_real_speed_and_unknown_total_behaviors(factory, known_size):
    make, origin = factory
    manager = make()
    origin.hold.add("/original")
    origin.advance.set()
    if not known_size:
        origin.mode["/original"] = "unknown_total"
    target = song()
    key = manager.task_key(target, OPTIONS)
    progress = []
    assert manager.download(target, {"on_progress": lambda _, percent: progress.append(percent)}, options=OPTIONS)

    def sample():
        row = manager.get_task(key)
        return row if row["bytes_written"] >= PAUSE_AFTER + 8192 and row["speed_bps"] > 0 else None

    observed = eventually(sample)
    assert observed["bytes_written"] == PAUSE_AFTER + 8192
    if known_size:
        assert observed["total_bytes"] == len(FIRST_AUDIO)
        assert observed["eta_seconds"] == pytest.approx((len(FIRST_AUDIO) - observed["bytes_written"]) / observed["speed_bps"])
        assert all(type(item) is int and item < 100 for item in progress)
    else:
        assert observed["total_bytes"] is None
        assert observed["eta_seconds"] is None
        assert progress and all(value is None for value in progress)
    origin.release.set()
    final = state(manager, key, "completed")
    assert final["total_bytes"] == len(FIRST_AUDIO)
    assert Path(final["path"]).read_bytes() == FIRST_AUDIO
    record("progress_real", known_size=known_size, total=observed["total_bytes"],
           bytes_written=observed["bytes_written"], speed_bps=observed["speed_bps"],
           eta_seconds=observed["eta_seconds"], progress=progress)


def test_junction_or_directory_symlink_is_not_a_download_destination(factory, tmp_path):
    make, origin = factory
    external = tmp_path / "protected"
    external.mkdir()
    preserved = external / "keep.txt"
    preserved.write_bytes(b"user data")
    link = tmp_path / "linked-media"
    if os.name == "nt":
        command = [os.environ.get("ComSpec", "cmd.exe"), "/d", "/c", "mklink", "/J", str(link), str(external)]
        created = subprocess.run(command, capture_output=True, timeout=5)
        if not link.is_dir():
            pytest.skip(f"Windows junction unavailable (exit {created.returncode})")
    else:
        link.symlink_to(external, target_is_directory=True)
    manager = make(directory=link)
    target = song()
    key = manager.task_key(target, OPTIONS)
    assert manager.download(target, options=OPTIONS)
    assert state(manager, key, "failed")["message"]
    assert origin.requests == []
    assert sorted(item.name for item in external.iterdir()) == ["keep.txt"]
    assert preserved.read_bytes() == b"user data"


def test_local_copy_lookup_preserves_same_title_other_recording(factory):
    make, origin = factory
    manager = make()
    origin.body["/live"] = SECOND_AUDIO
    studio, live = song(), song("live")
    for target in (studio, live):
        assert manager.download(target, options=OPTIONS)
    first = state(manager, manager.task_key(studio, OPTIONS), "completed")
    second = state(manager, manager.task_key(live, OPTIONS), "completed")
    assert first["path"] != second["path"]
    assert manager.find_download_path_for_song(song("unrelated")) == ""
    assert manager.find_download_path_for_song(live) == second["path"]
    assert not manager.remove_download(live["title"], live["artist"], song=live,
                                       expected_path=first["path"])[0]
    assert manager.remove_download(live["title"], live["artist"], song=live,
                                   expected_path=second["path"])[0]
    assert Path(first["path"]).read_bytes() == FIRST_AUDIO
    assert manager.get_task(manager.task_key(live, OPTIONS)) is None
