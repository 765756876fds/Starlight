"""Bounded CMS search posters; Qt async network, GUI-owned pixmaps, no legacy migration."""
from collections import OrderedDict, deque
import hashlib
import json
import logging
from pathlib import Path
import time

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QObject, QSaveFile, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QImageReader, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

logger = logging.getLogger(__name__)


def poster_key(source, resource_id, url):
    """Never reuse unqualified legacy vod_id files or expose endpoint/token data."""
    if resource_id is None or str(resource_id) == "" or not isinstance(url, str):
        return None
    url = url.strip()
    address = QUrl(url)
    if not address.isValid() or not address.host() or address.scheme() not in ("http", "https"):
        return None
    raw = json.dumps([source, str(resource_id), url], ensure_ascii=False, separators=(",", ":"))
    return "cms2-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SearchPosterLoader(QObject):
    ready = Signal(int, str, object)  # generation, opaque cache key, GUI-owned QPixmap
    MAX_ACTIVE = 2
    MAX_BYTES = 4 * 1024 * 1024
    MAX_EDGE = 4096
    MAX_PIXELS = 4_000_000
    capacity_available = Signal(int)
    MAX_PENDING = 100
    MAX_DEFERRED = 2000  # Metadata only; the search itself admits at most 2000 candidates.
    MAX_MEMORY = 128
    IDLE_MS = 8000
    TOTAL_MS = 12000
    FAILURE_SECONDS = 30

    def __init__(self, directory, parent=None, clock=time.monotonic):
        super().__init__(parent)
        self.directory = Path(directory)
        self._clock = clock
        self._generation = 0
        self._closed = False
        self._changing = False
        self._queue = deque()
        self._deferred = OrderedDict()
        self._pending = set()
        self._active = {}
        self._memory = OrderedDict()
        self._failures = OrderedDict()
        self._manager = QNetworkAccessManager(self)
        self._manager.finished.connect(self._finished)
        self.peak_active = 0

    def begin(self):
        """Invalidate old consumers and abort their replies without joining threads."""
        self._generation += 1
        self._changing = True
        self._queue.clear()
        self._deferred.clear()
        self._pending.clear()
        for reply in tuple(self._active):
            reply.abort()
        self._changing = False
        return self._generation

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.begin()

    def _path(self, key):
        # Keys are generated locally, never a path supplied by a CMS.
        if not isinstance(key, str) or len(key) != 69 or not key.startswith("cms2-"):
            raise ValueError("Invalid poster key")
        if any(c not in "0123456789abcdef" for c in key[5:]):
            raise ValueError("Invalid poster key")
        return self.directory / (key + ".png")

    def _decode(self, data):
        buffer = QBuffer()
        buffer.setData(QByteArray(data))
        buffer.open(QIODevice.ReadOnly)
        reader = QImageReader(buffer)
        if bytes(reader.format()).lower() not in (b"jpeg", b"png", b"webp", b"gif", b"bmp"):
            return None
        size = reader.size()
        if (not size.isValid() or size.width() > self.MAX_EDGE or size.height() > self.MAX_EDGE
                or size.width() * size.height() > self.MAX_PIXELS):
            return None
        reader.setScaledSize(size.scaled(QSize(140, 210), Qt.KeepAspectRatio))
        image = reader.read()
        return None if image.isNull() else QPixmap.fromImage(image)

    def _remember(self, key, pixmap):
        self._memory[key] = pixmap
        self._memory.move_to_end(key)
        while len(self._memory) > self.MAX_MEMORY:
            self._memory.popitem(last=False)

    def cached(self, key):
        if not key or self._closed:
            return None
        if key in self._memory:
            self._memory.move_to_end(key)
            return self._memory[key]
        try:
            path = self._path(key)
            if not path.is_file() or path.stat().st_size > self.MAX_BYTES:
                return None
            pixmap = self._decode(path.read_bytes())
            if pixmap is not None:
                self._remember(key, pixmap)
            return pixmap
        except (OSError, ValueError):
            return None

    def request(self, key, url):
        if not key or self._closed:
            return False
        try:
            self._path(key)
        except ValueError:
            return False
        url = url.strip()
        cached = self.cached(key)
        if cached is not None:
            self.ready.emit(self._generation, key, cached)
            return True
        failed_until = self._failures.get(key, 0)
        if key in self._pending or key in self._deferred:
            return True
        if self._clock() < failed_until:
            return False
        if len(self._queue) >= self.MAX_PENDING:
            if len(self._deferred) >= self.MAX_DEFERRED:
                return False
            self._deferred[key] = (self._generation, key, url)
            return True
        self._failures.pop(key, None)
        self._pending.add(key)
        self._queue.append((self._generation, key, url))
        self._pump()
        return True

    def prioritise(self, keys):
        """Reorder waiting metadata only; never interrupt either active transfer."""
        waiting = OrderedDict((job[1], job) for job in self._queue)
        waiting.update(self._deferred)
        ordered = []
        for key in keys:
            job = waiting.pop(key, None)
            if job is not None:
                ordered.append(job)
        ordered.extend(waiting.values())
        self._queue = deque(ordered[:self.MAX_PENDING])
        self._deferred = OrderedDict((job[1], job) for job in ordered[self.MAX_PENDING:])
        self._pending = {state["key"] for state in self._active.values()
                         if state["generation"] == self._generation}
        self._pending.update(job[1] for job in self._queue)

    def _pump(self):
        while self._deferred and len(self._queue) < self.MAX_PENDING:
            key, job = self._deferred.popitem(last=False)
            self._pending.add(key)
            self._queue.append(job)
        while (not self._closed and not self._changing and self._queue
               and len(self._active) < self.MAX_ACTIVE):
            generation, key, url = self._queue.popleft()
            request = QNetworkRequest(QUrl(url))
            request.setRawHeader(b"User-Agent", b"Mozilla/5.0 Starlight")
            request.setTransferTimeout(self.IDLE_MS)
            # Preserve ordinary CDN redirects, with a finite count and one total deadline.
            request.setAttribute(QNetworkRequest.RedirectPolicyAttribute,
                                 QNetworkRequest.NoLessSafeRedirectPolicy.value)
            request.setMaximumRedirectsAllowed(2)
            reply = self._manager.get(request)
            reply.setReadBufferSize(65536)
            timer = QTimer(reply)
            timer.setSingleShot(True)
            state = dict(generation=generation, key=key, data=bytearray(), timer=timer,
                         started=self._clock(), rejection=None)
            self._active[reply] = state
            self.peak_active = max(self.peak_active, len(self._active))
            reply.readyRead.connect(lambda r=reply: self._read(r))
            timer.timeout.connect(lambda r=reply: self._abort(r, "deadline"))
            timer.start(self.TOTAL_MS)

    def _abort(self, reply, reason):
        state = self._active.get(reply)
        if state is not None:
            state["rejection"] = reason
            reply.abort()

    def _read(self, reply):
        state = self._active.get(reply)
        if state is None:
            return
        mime = bytes(reply.rawHeader("Content-Type")).decode("ascii", "ignore").lower().split(";", 1)[0].strip()
        status = reply.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        length = reply.header(QNetworkRequest.ContentLengthHeader)
        if status is not None and 300 <= status < 400:
            return  # Qt follows at most two redirects inside the same total budget.
        if status != 200 or (mime and not mime.startswith("image/") and mime != "application/octet-stream"):
            self._abort(reply, "http_or_type")
            return
        if length is not None and int(length) > self.MAX_BYTES:
            self._abort(reply, "size")
            return
        # Consume at most the cap + one sentinel byte, even for a lying Content-Length.
        remaining = self.MAX_BYTES + 1 - len(state["data"])
        state["data"].extend(bytes(reply.read(max(0, remaining))))
        if len(state["data"]) > self.MAX_BYTES:
            self._abort(reply, "size")

    def _finished(self, reply):
        state = self._active.get(reply)
        if state is None:
            reply.deleteLater()
            return
        if state.get("finishing"):
            return
        state["finishing"] = True
        try:
            self._complete_reply(reply, state)
        except Exception as exc:
            if not self._closed and state["generation"] == self._generation:
                self._failures[state["key"]] = self._clock() + self.FAILURE_SECONDS
                while len(self._failures) > 256:
                    self._failures.popitem(last=False)
            logger.warning("movie_poster result=internal_error type=%s", type(exc).__name__)
        finally:
            state["timer"].stop()
            self._active.pop(reply, None)
            if state["generation"] == self._generation:
                self._pending.discard(state["key"])
            reply.deleteLater()
            self._pump()
            if not self._closed:
                self.capacity_available.emit(self._generation)

    def _complete_reply(self, reply, state):
        state["timer"].stop()
        current = not self._closed and state["generation"] == self._generation
        if current and reply.error() == QNetworkReply.NoError and not state["rejection"]:
            self._read(reply)
        key = state["key"]
        if current:
            self._pending.discard(key)
        pixmap = None
        if (current and reply.error() == QNetworkReply.NoError and not state["rejection"]
                and self._clock() - state["started"] < self.TOTAL_MS / 1000):
            pixmap = self._decode(state["data"])
            if self._clock() - state["started"] >= self.TOTAL_MS / 1000:
                pixmap = None
                state["rejection"] = "deadline"
        if current and pixmap is not None:
            self._remember(key, pixmap)
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                output = QSaveFile(str(self._path(key)))
                if output.open(QIODevice.WriteOnly):
                    if pixmap.save(output, "PNG"):
                        output.commit()
                    else:
                        output.cancelWriting()
            except OSError:
                pass  # Read-only disk degrades to validated in-memory thumbnails.
            self.ready.emit(self._generation, key, pixmap)
        elif current:
            self._failures[key] = self._clock() + self.FAILURE_SECONDS
            self._failures.move_to_end(key)
            while len(self._failures) > 256:
                self._failures.popitem(last=False)
        if current:
            logger.info("movie_poster result=%s elapsed_ms=%s bytes=%s",
                        "ready" if pixmap is not None else state["rejection"] or "network_or_decode",
                        round((self._clock() - state["started"]) * 1000), len(state["data"]))
