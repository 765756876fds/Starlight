import os
import time
import uuid
from contextlib import contextmanager
from unittest.mock import MagicMock, call

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtNetwork import QLocalSocket
from PySide6.QtTest import QSignalSpy, QTest

_app = QApplication.instance() or QApplication([])


def test_second_start_notifies_existing_instance_without_business_init(tmp_path):
    from app.ui.single_instance import SingleInstance

    primary = SingleInstance("test_starlight", lambda: None, data_dir=str(tmp_path))
    assert primary.acquire_or_notify() == "primary"
    assert primary._lock.staleLockTime() == 0
    try:
        secondary = SingleInstance("test_starlight", lambda: None, data_dir=str(tmp_path))
        assert secondary.acquire_or_notify(timeout_ms=500) == "secondary"
        assert secondary._owns_lock is False
    finally:
        primary.close()


def test_wake_message_reaches_primary(tmp_path):
    from app.ui.single_instance import SingleInstance

    called = MagicMock()
    instance = SingleInstance("test_starlight_wake", called, data_dir=str(tmp_path))
    assert instance.acquire_or_notify() == "primary"
    wake = QSignalSpy(instance.wake_requested)
    sender = None
    try:
        sender = SingleInstance("test_starlight_wake", lambda: None, data_dir=str(tmp_path))
        assert sender.acquire_or_notify(timeout_ms=500) == "secondary"
        _wait_for_signal(wake)
        called.assert_called_once_with()
    finally:
        if sender is not None:
            sender.close()
        instance.close()


def test_wake_main_window_restores_and_activates():
    from main import Application

    app = Application.__new__(Application)
    window = MagicMock()
    window.isMinimized.return_value = True
    window.isVisible.return_value = True
    window.windowState.return_value = Qt.WindowNoState
    app._main_window = window
    app._wake_main_window()
    window.showNormal.assert_called_once_with()
    window.raise_.assert_called_once_with()
    window.activateWindow.assert_called_once_with()


def _wait_for_signal(spy):
    if not spy.count():
        assert spy.wait(1000), "Qt signal did not arrive within 1 second"


def _wait_until(predicate):
    deadline = time.monotonic() + 1.0
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate(), "Qt socket state did not settle within 1 second"


@pytest.fixture
def wake_server(tmp_path):
    from app.ui.single_instance import SingleInstance

    called = MagicMock()
    instance = SingleInstance("test_wake_" + uuid.uuid4().hex, called, data_dir=str(tmp_path))
    assert instance.acquire_or_notify() == "primary"
    try:
        yield instance, called
    finally:
        instance.close()


@contextmanager
def _connected_peer(instance):
    peer = QLocalSocket()
    peer.connectToServer(instance.server_name)
    try:
        assert peer.waitForConnected(1000), peer.errorString()
        yield peer
    finally:
        peer.abort()


def _send(peer, payload):
    assert peer.write(payload) == len(payload)
    peer.flush()


@pytest.mark.parametrize("disconnect_before_accept", [False, True])
def test_wake_buffered_before_ready_read_is_connected(wake_server, disconnect_before_accept):
    instance, called = wake_server
    # Keep real Qt I/O running while delaying only application-level acceptance.
    instance._server.blockSignals(True)
    with _connected_peer(instance) as peer:
        _send(peer, b"WAKE\n")
        if disconnect_before_accept:
            peer.disconnectFromServer()
        _wait_until(lambda: any(s.bytesAvailable() == 5
                               for s in instance._server.findChildren(QLocalSocket)))
        called.assert_not_called()
        wake = QSignalSpy(instance.wake_requested)
        instance._on_connection()
        _wait_for_signal(wake)
        QTest.qWait(20)
        called.assert_called_once_with()


def test_wake_arriving_after_ready_read_is_connected_runs_on_gui_thread(wake_server):
    instance, called = wake_server
    callback_threads = []
    called.side_effect = lambda: callback_threads.append(QThread.currentThread())
    with _connected_peer(instance) as peer:
        _wait_until(lambda: len(instance._sockets) == 1)
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, b"WAKE\n")
        _wait_for_signal(wake)
        called.assert_called_once_with()
        assert callback_threads == [_app.thread()]


@pytest.mark.parametrize("fragments", [
    (b"W", b"A", b"K", b"E", b"\n"),
    (b"WA", b"KE\n"),
    (b"WAKE", b"\n"),
])
def test_fragmented_wake_stays_buffered_until_newline(wake_server, fragments):
    instance, called = wake_server
    with _connected_peer(instance) as peer:
        _wait_until(lambda: len(instance._sockets) == 1)
        socket = instance._sockets[0]
        buffered = 0
        for fragment in fragments[:-1]:
            ready = QSignalSpy(socket.readyRead)
            _send(peer, fragment)
            _wait_for_signal(ready)
            buffered += len(fragment)
            assert socket.bytesAvailable() == buffered
            called.assert_not_called()
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, fragments[-1])
        _wait_for_signal(wake)
        called.assert_called_once_with()


def test_partial_wake_buffered_before_accept_can_finish_later(wake_server):
    instance, called = wake_server
    instance._server.blockSignals(True)
    with _connected_peer(instance) as peer:
        _send(peer, b"WA")
        _wait_until(lambda: any(s.bytesAvailable() == 2
                               for s in instance._server.findChildren(QLocalSocket)))
        instance._on_connection()
        called.assert_not_called()
        assert instance._sockets[0].bytesAvailable() == 2
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, b"KE\n")
        _wait_for_signal(wake)
        called.assert_called_once_with()


def test_delayed_accept_reads_wake_even_after_peer_disconnected(wake_server):
    instance, called = wake_server
    instance._server.newConnection.disconnect(instance._on_connection)
    instance._server.newConnection.connect(lambda: QTimer.singleShot(30, instance._on_connection))
    with _connected_peer(instance) as peer:
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, b"WAKE\n")
        peer.disconnectFromServer()
        _wait_for_signal(wake)
        QTest.qWait(20)
        called.assert_called_once_with()


def test_rechecking_consumed_buffer_does_not_repeat_wake(wake_server):
    instance, called = wake_server
    with _connected_peer(instance) as peer:
        _wait_until(lambda: len(instance._sockets) == 1)
        socket = instance._sockets[0]
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, b"WAKE\n")
        _wait_for_signal(wake)
        instance._read_socket(socket)
        instance._read_socket(socket)
        QTest.qWait(20)
        called.assert_called_once_with()


def test_several_complete_wake_messages_in_one_read_are_each_delivered(wake_server):
    instance, called = wake_server
    with _connected_peer(instance) as peer:
        _send(peer, b"WAKE\nWAKE\nWAKE\n")
        _wait_until(lambda: called.call_count == 3)
        QTest.qWait(20)
        assert called.call_args_list == [call(), call(), call()]


def test_sequential_wake_messages_on_one_socket_are_each_delivered(wake_server):
    instance, called = wake_server
    with _connected_peer(instance) as peer:
        for count in range(1, 4):
            wake = QSignalSpy(instance.wake_requested)
            _send(peer, b"WAKE\n")
            _wait_for_signal(wake)
            assert called.call_count == count
        assert called.call_args_list == [call(), call(), call()]


def test_consecutive_connections_wake_once_each_and_are_released(wake_server):
    instance, called = wake_server
    for count in range(1, 4):
        with _connected_peer(instance) as peer:
            wake = QSignalSpy(instance.wake_requested)
            _send(peer, b"WAKE\n")
            _wait_for_signal(wake)
            assert called.call_count == count
        _wait_until(lambda: not instance._sockets)
    assert called.call_args_list == [call(), call(), call()]


def test_disconnected_partial_message_never_wakes_and_does_not_block_next_peer(wake_server):
    instance, called = wake_server
    with _connected_peer(instance) as peer:
        _wait_until(lambda: len(instance._sockets) == 1)
        ready = QSignalSpy(instance._sockets[0].readyRead)
        _send(peer, b"WAKE")
        _wait_for_signal(ready)
        called.assert_not_called()
    _wait_until(lambda: not instance._sockets)
    with _connected_peer(instance) as peer:
        wake = QSignalSpy(instance.wake_requested)
        _send(peer, b"WAKE\n")
        _wait_for_signal(wake)
        called.assert_called_once_with()
