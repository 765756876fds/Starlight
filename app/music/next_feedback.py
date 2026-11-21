"""Observe one voice next request; never select songs or drive playback.

The controller keeps its legacy return contract. Only explicit stage evidence
and a new MPV playlist entry with advancing time can confirm this operation.
"""

from contextvars import ContextVar

import math
import os
import threading
import time
import uuid
from dataclasses import dataclass

from ..core.notifications import current_operation_id, get_notification_center
from ..core.operation_result import OperationResult, OperationStatus as S


@dataclass
class Request:
    operation: str
    owner: str
    thread: int
    before: int
    previous_entry: int | None
    deadline: float
    epoch: int | None = None
    result: OperationResult = OperationResult(S.ACCEPTED, "正在选择下一首")
    terminal: bool = False
    accepted: bool = False
    position: float | None = None
    ready: bool = False


# Only callbacks on the thread executing this particular manual play are observed.
_manual_receipt = ContextVar("manual_music_receipt", default=None)


def report(controller, stage, result, epoch=None):
    callback = _manual_receipt.get()
    if callback is not None:
        try:
            callback(controller, stage, result, epoch)
        except RuntimeError:
            pass  # The receiving Qt widget may already have been destroyed.
    admission = getattr(controller, "_goal_admission", None)
    if admission is not None:
        try:
            admission.report(stage, result, epoch)
        except Exception:
            pass  # Optional receipt observation must not alter old playback.
    observer = getattr(controller, "_next_feedback", None)
    if observer is not None:
        try:
            observer.report(stage, result, epoch)
        except Exception:
            # Observability cannot change the old playback contract.
            return


class NextFeedback:
    def __init__(self, controller, *, budget=30.0, interval=0.15):
        self.controller = controller
        self.budget, self.interval = budget, interval
        self._lock = threading.RLock()
        self._calling = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._request = None
        self._closed = False

    def _property(self, name):
        player = self.controller.mpv
        # Real MPV's ordinary property lock has no acquisition deadline. This
        # read-only observer uses its existing RPC under a bounded lock instead.
        lock = getattr(player, "_pipe_lock", None)
        rpc = getattr(player, "_send_command", None)
        if lock is not None and callable(rpc):
            if not lock.acquire(timeout=0.04):
                return None
            try:
                reply = rpc("get_property", [name], timeout=0.04, _locked=True)
                return reply.get("data") if isinstance(reply, dict) and reply.get("error") == "success" else None
            finally:
                lock.release()
        return player.get_property(name, timeout=0.04)

    def _entry(self):
        rows = self._property("playlist")
        if not isinstance(rows, list):
            return None
        for row in rows:
            if isinstance(row, dict) and row.get("current") is True and type(row.get("id")) is int:
                return row
        return None

    def _publish(self, req):
        if not req.operation:
            return
        center = get_notification_center()
        record = center.get(req.operation)
        if record is None or record.result_owner != req.owner:
            return
        value = req.result
        center.update(req.operation, title=value.message, message="", advice="",
                      status="success" if value.status is S.CONFIRMED else
                      "failed" if value.status is S.FAILED else
                      "warning" if req.terminal else "processing",
                      phase="done" if req.terminal else "running")

    def _finish(self, req, status, message):
        req.result = OperationResult(status, message)
        req.terminal = True

    def perform(self, command):
        if not self._calling.acquire(timeout=0.2):
            return OperationResult(S.UNAVAILABLE, "正在处理切歌，请稍后再试")
        try:
            current = command.is_current or (lambda: True)
            if self._closed or not current():
                return OperationResult(S.UNKNOWN, "这次切歌请求已过期")
            try:
                row = self._entry()
            except Exception:
                row = None
            operation = command.notification_id or current_operation_id()
            req = Request(operation, uuid.uuid4().hex, threading.get_ident(),
                          self.controller._play_request_id,
                          row["id"] if row else None, time.monotonic() + self.budget)
            with self._lock:
                old = self._request
                if old is not None and not old.terminal:
                    self._finish(old, S.UNKNOWN, "后续操作已覆盖这次切歌，未继续确认")
                    self._publish(old)
                self._request = req
                if operation:
                    get_notification_center().update(operation, result_owner=req.owner)
            try:
                self.controller.next(_timeout=0.2, _voice_guard=current)
            except Exception:
                with self._lock:
                    self._finish(req, S.FAILED, "切换未成功，请检查播放器或重试")
            with self._lock:
                req.ready = True
                if req.epoch is None and not req.terminal:
                    self._finish(req, S.UNKNOWN, "尚未确认切歌结果，请查看播放页面")
                self._publish(req)
                if not req.terminal and operation:
                    self._start_locked()
                return req.result
        finally:
            self._calling.release()

    def report(self, stage, result, epoch):
        # Called under the existing playback lock: no IPC, Qt, or notification
        # callbacks here, and no waiting for the observer thread.
        with self._lock:
            req = self._request
            if req is None or req.terminal:
                return
            if stage == "voice_next" and threading.get_ident() == req.thread:
                endings = {
                    "unavailable": (S.UNAVAILABLE, "播放器尚未就绪，未切换歌曲"),
                    "debounced": (S.UNAVAILABLE, "切歌太快，本次重复请求已忽略"),
                    "busy": (S.UNAVAILABLE, "正在处理播放，请稍后再切歌"),
                    "stale": (S.UNKNOWN, "这次切歌请求已过期"),
                    "external": (S.ACCEPTED, "外部播放器已接受切歌，起播尚未确认"),
                    "empty": (S.UNAVAILABLE, "队列为空，没有下一首"),
                    "end": (S.UNAVAILABLE, "已到队列末尾，没有下一首"),
                }
                if result in endings:
                    self._finish(req, *endings[result])
            elif stage == "voice_selected" and threading.get_ident() == req.thread and req.epoch is None:
                req.epoch = epoch
                req.result = OperationResult(S.ACCEPTED, "已选中下一首，正在解析并加载")
            elif req.epoch is not None and epoch == req.epoch:
                if stage == "loadfile":
                    if result == "confirmed":
                        req.accepted = True
                        req.result = OperationResult(S.ACCEPTED, "播放器已接受下一首，正在确认起播")
                    elif result == "unknown":
                        self._finish(req, S.UNKNOWN, "播放器未返回加载确认，请查看播放状态或重试")
                    else:
                        self._finish(req, S.FAILED, "下一首加载未成功，请换一首或重试")
                elif stage == "resolve" and result in ("unavailable", "no_url", "failed"):
                    self._finish(req, S.FAILED, "下一首地址解析失败，正在按原有规则处理，请检查音源")
                elif stage == "voice_load_failed":
                    self._finish(req, S.FAILED, "下一首未能加载，请检查文件或播放器")
            self._wake.set()

    def _start_locked(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="music-next-feedback", daemon=True)
            self._thread.start()
        self._wake.set()

    def playing_sample(self):
        """Bounded IPC evidence for the currently selected media; worker thread only."""
        try:
            row = self._entry()
            song = self.controller.get_current_song()
            expected = song.get("path") if isinstance(song, dict) else None
            actual = row.get("filename") if row else None
            if not isinstance(expected, str) or not isinstance(actual, str):
                return
            matching = actual == expected or (not expected.startswith(("http:", "https:")) and
                                              os.path.normcase(actual) == os.path.normcase(expected))
            if not matching:
                return
            idle, paused, pos = (self._property(n) for n in ("idle-active", "pause", "time-pos"))
            if (idle is not False or paused is not False or type(pos) not in (int, float)
                    or not math.isfinite(pos) or pos < 0):
                return
            return row["id"], pos
        except Exception:
            return None

    def _advance(self, req):
        c = self.controller
        center = get_notification_center()
        if (self._closed or center.get(req.operation) is None or c._play_request_id != req.epoch or
                center.current_voice_id not in ("", req.operation)):
            return S.UNKNOWN, "后续操作已覆盖这次切歌，未继续确认", None
        if time.monotonic() >= req.deadline:
            return (S.UNKNOWN if req.accepted else S.TIMEOUT,
                    "播放器已接受加载，但尚未确认起播，请查看播放页面" if req.accepted else
                    "下一首解析或加载超时，请检查音源后重试", None)
        if not req.accepted:
            return
        try:
            sample = self.playing_sample()
            if sample is None or sample[0] == req.previous_entry:
                return
            _, pos = sample
            if req.position is not None and pos > req.position:
                return S.CONFIRMED, "已确认下一首开始播放", pos
            return None, "", pos
        except Exception:
            # Missing IPC evidence cannot be upgraded to successful playback.
            return

    def _run(self):
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            with self._lock:
                req = self._request
                if req is None or self._closed:
                    self._thread = None
                    return
                if req.terminal:
                    self._publish(req)
                    self._thread = None
                    return
                if not req.ready:
                    continue  # An existing observer may see the next call before selection.
            # Read-only bounded IPC outside both the controller and observer lock.
            observation = self._advance(req)
            with self._lock:
                if self._request is req:
                    # Check currency again after IPC; a late sample cannot
                    # confirm a different song or an already superseded request.
                    if not req.terminal:
                        if (self._closed or self.controller._play_request_id != req.epoch or
                                get_notification_center().current_voice_id not in ("", req.operation)):
                            self._finish(req, S.UNKNOWN, "播放内容已变化，这次切歌已过期")
                        elif observation is not None:
                            status, message, position = observation
                            if status is not None:
                                self._finish(req, status, message)
                            if position is not None:
                                req.position = position
                    self._publish(req)

    def close(self):
        with self._lock:
            self._closed = True
            if self._request is not None and not self._request.terminal:
                self._finish(self._request, S.UNKNOWN, "播放器已关闭，未继续确认切歌")
                self._publish(self._request)
            thread = self._thread
            self._wake.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(0.5)


def observe_manual_play(controller, target, args, cancel, publish, *, budget=8.0):
    """Observe the existing command on its UI worker. Never load or retry media here."""
    observer = getattr(controller, "_next_feedback", None)
    previous_ids = None
    if isinstance(observer, NextFeedback):
        try:
            playlist = observer._property("playlist")
            if isinstance(playlist, list):
                previous_ids = {row["id"] for row in playlist}
        except Exception:
            pass

    epoch = load_result = None

    def receipt(owner, stage, result, request):
        nonlocal epoch, load_result
        if owner is not controller or cancel.is_set():
            return
        epoch = request
        if stage == "resolve" and result == "ready":
            publish("loading", request)
        elif stage == "resolve" and result == "no_url":
            load_result = "no_url"
        elif stage == "voice_load_failed":
            load_result = "failed"
        elif stage == "loadfile":
            load_result = result
            if result == "confirmed":
                publish("accepted", request)

    if cancel.is_set():
        return
    token = _manual_receipt.set(receipt)
    try:
        result = target(*args)
    except Exception:
        result = False
    finally:
        _manual_receipt.reset(token)
    if cancel.is_set():
        return
    if load_result != "confirmed":
        if load_result in ("failed", "no_url") or (load_result is None and result is False):
            publish("no_url" if load_result == "no_url" else "failed", epoch)
        else:
            publish("unknown", epoch)
        return

    deadline = time.monotonic() + budget
    last = None
    while not cancel.is_set() and time.monotonic() < deadline:
        if controller._play_request_id != epoch or not controller._running or not controller._movie_permits_request():
            return
        sample = observer.playing_sample() if previous_ids is not None else None
        if sample is not None and sample[0] not in previous_ids:
            now = time.monotonic()
            # A seek or resume jump alone does not establish continuous playback.
            if (last is not None and last[0] == sample[0]
                    and 0 < sample[1] - last[1] <= 4 * (now - last[2]) + .25):
                publish("started", epoch)
                return
            last = (*sample, now)
        else:
            last = None
        if cancel.wait(0.15):
            return
    if not cancel.is_set():
        publish("unknown", epoch)
