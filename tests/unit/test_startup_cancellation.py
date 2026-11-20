"""Deterministic shutdown races. No real model, system service or music library."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest

from app.music.controller import MusicController
from app.music.mpv import MpvController
from app.music.scanner import MusicScanner
from app.voice import asr_bootstrap as boot
from app.voice.runtime import VoiceRuntime
from main import Application

pytestmark = pytest.mark.unit


@pytest.fixture
def backend(tmp_path, monkeypatch):
    model = tmp_path / "test.gguf"
    model.touch()
    monkeypatch.setattr(boot, "_server_proc", None)
    monkeypatch.setattr(boot, "_server_backend", "")
    monkeypatch.setattr(boot, "_startup_cancel", None)
    monkeypatch.setattr(boot, "_atexit_registered", True)
    monkeypatch.setattr(boot.tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(boot, "_is_server_up", Mock(return_value=False))
    monkeypatch.setattr(boot, "_port_in_use", Mock(return_value=False))
    monkeypatch.setattr(boot, "ensure_assets", Mock(return_value=True))
    monkeypatch.setattr(boot, "resolve_model_paths", Mock(return_value=(str(model), "")))
    monkeypatch.setattr(boot, "find_llama_backend_candidates",
                        Mock(return_value=[{"exe": "test.exe", "backend": "test"}]))
    monkeypatch.setattr(boot.subprocess, "Popen", Mock(side_effect=AssertionError("unexpected process")))
    return boot


def owned_process():
    process = Mock()
    process.poll.return_value = None
    process.wait.side_effect = lambda timeout: setattr(process.poll, "return_value", 1) or 1
    return process


def test_shutdown_during_preparation_cannot_create_a_backend(backend):
    entered, release, cancel = Event(), Event(), Event()

    def assets(config, *, cancel):
        entered.set()
        assert release.wait(3)
        return True  # Preparation reported success after shutdown.

    backend.ensure_assets.side_effect = assets
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(backend.ensure_and_launch, {}, cancel=cancel)
        try:
            assert entered.wait(3)
            assert backend.ensure_and_launch({}) is None
            backend.ensure_assets.assert_called_once()
            backend.shutdown_server()
            assert cancel.is_set()
        finally:
            release.set()
        assert pending.result(3) is None
    assert backend.ensure_and_launch({}, cancel=cancel) is None
    backend.subprocess.Popen.assert_not_called()


def test_cancel_before_backend_popen(backend):
    cancel = Event()
    candidates = backend.find_llama_backend_candidates.return_value
    backend.find_llama_backend_candidates.side_effect = lambda _: cancel.set() or candidates
    assert backend.ensure_and_launch({}, cancel=cancel) is None
    backend.subprocess.Popen.assert_not_called()


def test_shutdown_during_popen_reaps_returned_handle(backend):
    entered, release, cancel = Event(), Event(), Event()
    process = owned_process()

    def popen(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return process

    backend.subprocess.Popen.side_effect = popen
    with ThreadPoolExecutor(max_workers=2) as workers:
        pending = workers.submit(backend.ensure_and_launch, {}, cancel=cancel)
        try:
            assert entered.wait(3)
            cleanup = workers.submit(backend.shutdown_server)
            assert cancel.wait(3)
            assert not cleanup.done()  # The admitted creation has not handed off ownership.
        finally:
            release.set()
        assert pending.result(3) is None
        cleanup.result(3)
    backend.shutdown_server()
    process.terminate.assert_called_once()
    assert process.poll() == 1
    assert backend._server_proc is None


def test_shutdown_reaps_registered_backend_before_health_probe_returns(backend, monkeypatch):
    probing, finish_probe = Event(), Event()
    process = owned_process()
    backend.subprocess.Popen.side_effect = None
    backend.subprocess.Popen.return_value = process

    def health(timeout=0.8):
        if timeout == 3:
            probing.set()
            assert finish_probe.wait(3)
            return True
        return False

    backend._is_server_up.side_effect = health
    monkeypatch.setattr(backend, "_wait", lambda delay, cancel: cancel.is_set())
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(backend.ensure_and_launch, {})
        try:
            assert probing.wait(3)
            assert backend._server_proc is process
            backend.shutdown_server()
            assert process.poll() == 1
        finally:
            finish_probe.set()
        assert pending.result(3) is None
    backend.subprocess.Popen.assert_called_once()
    assert backend._server_proc is None


def test_external_health_never_claims_process(backend):
    backend._is_server_up.return_value = True
    assert backend.ensure_and_launch({}) is None
    backend.shutdown_server()
    assert backend._server_proc is None
    backend.ensure_assets.assert_not_called()
    backend.subprocess.Popen.assert_not_called()


def test_cancelled_download_discards_only_its_partial_file(monkeypatch, tmp_path):
    cancel = Event()
    dest = tmp_path / "model"
    existing = tmp_path / "other.part"
    existing.write_bytes(b"preserve")
    response = Mock(status_code=200, headers={})

    def chunks(chunk_size):
        yield b"first"
        cancel.set()
        yield b"late"

    response.iter_content.side_effect = chunks
    stream = Mock()
    stream.__enter__ = Mock(return_value=response)
    stream.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(boot.requests, "get", Mock(return_value=stream))
    assert boot.download_file("http://unused.invalid", dest, cancel=cancel) is False
    assert list(tmp_path.iterdir()) == [existing]
    assert existing.read_bytes() == b"preserve"
    stream.__exit__.assert_called_once()


def test_cancelled_seed_copy_not_published(monkeypatch, tmp_path):
    cancel = Event()
    user = tmp_path / "user"
    seed = tmp_path / "seed"
    seed.mkdir()
    model = seed / next(iter(boot._MODEL_URLS))
    model.write_bytes(b"only a test")
    monkeypatch.setattr(boot, "USER_MODEL_DIR", user)
    monkeypatch.setattr(boot, "SEED_MODEL_DIR", seed)
    monkeypatch.setattr(boot.shutil, "copystat", lambda *_: cancel.set())
    boot._seed_models_if_needed(cancel)
    assert list(user.iterdir()) == []
    assert model.read_bytes() == b"only a test"


def test_loading_exit_prevents_start_and_late_callback(monkeypatch):
    import sys

    app = Application.__new__(Application)
    app._running = False
    app._shutdown_event = Event()
    stop = app.stop
    start = app.start
    app._single_instance = Mock()
    app.automation = None
    for name in ("voice", "speech_judge", "speech_queue", "tts", "lol", "music",
                 "event_bus", "scheduler", "state_store", "event_log"):
        setattr(app, name, Mock())
    app.initialize = Mock(side_effect=stop)
    app.start = Mock()
    monkeypatch.setattr(sys, "argv", ["main.py", "--headless"])
    monkeypatch.setattr("app.core.log_redirect.close_redirect", Mock())
    monkeypatch.setattr(boot, "shutdown_server", Mock())
    assert app.run() == 0
    app.start.assert_not_called()
    start()  # A late callback after cleanup.
    stop()
    app.scheduler.stop.assert_called_once()
    app.scheduler.start.assert_not_called()
    app.music.start.assert_not_called()
    app.voice.start.assert_not_called()
    boot.shutdown_server.assert_called_once()


def test_voice_initialization_return_after_exit_cannot_create_workers():
    cancel = Event()
    runtime = VoiceRuntime.__new__(VoiceRuntime)
    runtime._running = False
    runtime._observations = Mock()
    runtime.intent_router = Mock()
    runtime.asr = Mock()
    runtime.asr.initialize.side_effect = lambda **kwargs: cancel.set()
    runtime.start(bootstrap_result="ok", cancel=cancel)
    assert not hasattr(runtime, "_asr_thread")
    assert not hasattr(runtime, "_interrupt_thread")
    runtime.start(bootstrap_result="ok", cancel=cancel)
    runtime.asr.initialize.assert_called_once_with(bootstrap_result="ok", cancel=cancel)


def test_music_ipc_return_after_exit_does_not_start_workers():
    cancel = Event()
    controller = MusicController.__new__(MusicController)
    controller._running = False
    controller.history_recorder = Mock()
    controller.mpv = Mock()
    controller.mpv.start.return_value = True
    controller.mpv.get_property.side_effect = lambda *a, **k: cancel.set() or True
    controller._subscribe_speech_events = Mock()
    controller.scanner = Mock()
    controller.default_volume = 50
    assert controller.start(cancel=cancel) is False
    assert not hasattr(controller, "_idle_thread")
    controller.scanner.start_highlight_background.assert_not_called()


@pytest.mark.parametrize("scanner", [False, True])
def test_mpv_created_during_exit_is_reaped(monkeypatch, scanner):
    cancel = Event()
    process = owned_process()
    monkeypatch.setattr("subprocess.Popen", Mock(side_effect=lambda *a, **k: cancel.set() or process))
    if scanner:
        library = MusicScanner.__new__(MusicScanner)
        library.mpv_path = "fake-mpv.exe"
        assert library._verify_playable("generated.wav", cancel=cancel) is False
        process.kill.assert_called_once()
    else:
        mpv = MpvController("fake-mpv.exe")
        assert mpv._launch(cancel=cancel) is False
        assert mpv._process is None
        process.terminate.assert_called_once()
    assert process.poll() == 1
