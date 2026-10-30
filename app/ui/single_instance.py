"""Windows GUI 单实例锁与已有窗口唤醒通信。"""
from __future__ import annotations

import getpass
import logging
import os
from pathlib import Path

from PySide6.QtCore import QObject, QLockFile, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

logger = logging.getLogger(__name__)


class SingleInstance(QObject):
    """用文件锁保证互斥，用 Qt 本地 socket 向主实例发送唤醒消息。"""

    wake_requested = Signal()

    def __init__(self, name: str, on_wake, data_dir: str | None = None, parent=None):
        super().__init__(parent)
        user = getpass.getuser() or "user"
        safe_user = "".join(ch if ch.isalnum() else "_" for ch in user)
        self.server_name = f"{name}_{safe_user}"
        root = Path(data_dir or os.environ.get("LOCALAPPDATA") or Path.home())
        lock_dir = root / "Starlight"
        lock_dir.mkdir(parents=True, exist_ok=True)
        self._lock = QLockFile(str(lock_dir / f"{name}.instance.lock"))
        # 长期运行的桌面实例不能按锁文件年龄过期；Qt 仍会依据锁内
        # PID/进程信息处理异常退出遗留锁。
        self._lock.setStaleLockTime(0)
        self._lock_name = self._lock.fileName()
        self._owns_lock = False
        self._server = None
        self._sockets = []
        self.wake_requested.connect(on_wake)

    def acquire_or_notify(self, timeout_ms: int = 250) -> str:
        """返回 primary、secondary 或 unavailable，避免混淆唤醒与主实例。"""
        if self._lock.tryLock(timeout_ms):
            self._owns_lock = True
            logger.info("[SingleInstance] lock acquired name=%s", Path(self._lock_name).name)
            self._server = QLocalServer(self)
            if not self._server.listen(self.server_name):
                logger.error("[SingleInstance] server listen failed name=%s error=%s",
                             self.server_name, self._server.errorString())
                QLocalServer.removeServer(self.server_name)
                if not self._server.listen(self.server_name):
                    logger.error("[SingleInstance] server retry failed name=%s error=%s",
                                 self.server_name, self._server.errorString())
                    self._lock.unlock()
                    self._owns_lock = False
                    return "unavailable"
            logger.info("[SingleInstance] server listening name=%s", self.server_name)
            self._server.newConnection.connect(self._on_connection)
            return "primary"

        socket = QLocalSocket(self)
        logger.info("[SingleInstance] lock busy name=%s; connecting to existing server",
                     Path(self._lock_name).name)
        socket.connectToServer(self.server_name)
        if not socket.waitForConnected(timeout_ms):
            logger.error("[SingleInstance] wake connect failed name=%s error=%s",
                         self.server_name, socket.errorString())
            return "unavailable"
        if socket.write(b"WAKE\n") <= 0:
            logger.error("[SingleInstance] wake write failed name=%s error=%s",
                         self.server_name, socket.errorString())
            socket.disconnectFromServer()
            return "unavailable"
        socket.flush()
        socket.disconnectFromServer()
        logger.info("[SingleInstance] wake sent name=%s", self.server_name)
        return "secondary"

    def _on_connection(self):
        while self._server and self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._sockets.append(socket)
            socket.readyRead.connect(lambda s=socket: self._read_socket(s))

    def _read_socket(self, socket):
        if b"WAKE" in bytes(socket.readAll()):
            logger.info("[SingleInstance] wake received name=%s", self.server_name)
            self.wake_requested.emit()
        socket.disconnectFromServer()
        try:
            self._sockets.remove(socket)
        except ValueError:
            pass

    def close(self):
        if self._server is not None:
            for socket in list(self._sockets):
                socket.disconnectFromServer()
            self._sockets.clear()
            self._server.close()
            QLocalServer.removeServer(self.server_name)
            self._server = None
        if self._owns_lock and self._lock.isLocked():
            self._lock.unlock()
            logger.info("[SingleInstance] lock released name=%s", Path(self._lock_name).name)
        self._owns_lock = False
