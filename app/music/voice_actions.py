"""Guarded adapters to existing music operations; no second playback system."""

from contextlib import contextmanager

from ..core.operation_result import OperationResult, OperationStatus as S
from .preferences import media_key


@contextmanager
def playback_lock(lock, guard):
    acquired = lock.acquire(timeout=0.2) if guard is not None else lock.acquire()
    try:
        yield acquired
    finally:
        if acquired:
            lock.release()


def perform(controller, command):
    current = command.is_current or (lambda: True)
    action = command.get("action")
    if action not in ("pause", "resume", "next", "previous", "stop"):
        return OperationResult(S.UNSUPPORTED, "暂不支持这项播放操作")
    before = controller._play_request_id
    method = {"previous": controller.prev, "stop": controller.stop_current}.get(action) or getattr(
        controller, action
    )
    if not current():
        return OperationResult(S.UNKNOWN, "播放内容已变化，请重新说一次")
    if action in ("next", "pause", "resume"):
        value = (
            method(_timeout=0.2, _voice_guard=current)
            if action == "next"
            else method(_voice_guard=current)
        )
    else:
        value = method(_voice_guard=current)
    if value is False:
        return OperationResult(S.FAILED, "播放操作没有完成，请重试")
    if action in ("next", "previous"):
        changed = controller._play_request_id != before
        return OperationResult(
            S.ACCEPTED if changed else S.UNKNOWN,
            "正在切换歌曲" if changed else "暂未切换歌曲，请检查队列或稍后重试",
        )
    return value


def save(controller, command):
    song = command.get("song")
    if (
        not isinstance(song, dict)
        or not media_key(song)
        or command.is_current is not None
        and not command.is_current()
    ):
        return OperationResult(S.UNKNOWN, "当前歌曲已变化，请重新说一次")
    song = dict(song)
    if song.get("source") == "online" and song.get("source_name") and song.get("track_id"):
        song["source_type"] = "online"
    ok = controller.db.save_liked_song(song, command.is_current)
    return OperationResult(
        S.CONFIRMED if ok else S.FAILED,
        "已保存到我喜欢的音乐" if ok else "暂未保存歌曲，播放仍可继续，请稍后重试",
    )


def feedback(controller, command):
    song = command.get("song")
    if not isinstance(song, dict) or command.is_current is not None and not command.is_current():
        return OperationResult(S.UNKNOWN, "当前歌曲已变化，请重新说一次")
    # Explicit like uses the existing preference API. Weak dislike lives in the
    # separate, revocable interaction ledger and never overwrites this value.
    return controller.recommendation_feedback("track", media_key(song), positive=True)
