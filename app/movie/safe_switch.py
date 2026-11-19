"""Owns the media identity and the single, user-approved MPV handoff."""
from dataclasses import dataclass
import time
from urllib.parse import urlsplit

from .cms_parser import episode_eps, normalise_playback
from .playback_diagnostics import number, read_sample
from .progressive_search import work_identity
from .switch_probe import origin


@dataclass(frozen=True)
class SwitchContext:
    generation: int
    index: int
    attempt: int
    process: object
    item: dict
    original: str
    alternative: dict
    target: str


def _film_url(item):
    kind = str(item.get("type_name") or item.get("vod_type") or "").strip().casefold()
    if kind not in {"电影", "movie", "动作片", "喜剧片", "科幻片", "剧情片", "爱情片",
                    "恐怖片", "战争片", "纪录片", "动画电影", "悬疑片", "犯罪片"}:
        return None
    episodes = episode_eps(item)
    if (len(episodes) != 1 or episodes[0][0].casefold() not in {"正片", "全片", "hd", "movie"}
            or len(item.get("_playback_lines") or ()) > 1):
        return None
    url = episodes[0][1]
    try:
        origin(url)  # Reject malformed ports before construction of a context.
        parts = urlsplit(url)
        if (parts.scheme in ("http", "https") and parts.hostname and parts.username is None
                and parts.path.lower().endswith(".m3u8") and not parts.fragment and len(url) <= 2048):
            return url
    except ValueError:
        pass
    return None


def context_for(controller, group):
    current = controller.current_item()
    if not current or controller._current_index != 0 or not current.get("_source_id"):
        return None
    work = work_identity(current)
    original = _film_url(current)
    process = getattr(controller.mpv, "_process", None)
    if not work or not original or process is None:
        return None
    for row in group:
        if (not row.get("_source_id") or row["_source_id"] == current["_source_id"]
                or work_identity(row) != work):
            continue
        alternative = normalise_playback(row)
        target = _film_url(alternative)
        if target and origin(original) != origin(target):
            return SwitchContext(generation=controller._playback_gen, index=0,
                                 attempt=controller._play_attempt, process=process,
                                 item=current, original=original, alternative=alternative, target=target)
    return None


def context_current(controller, context):
    return (not getattr(controller, "_media_voice_closed", False)
            and controller._playback_gen == context.generation
            and controller._current_index == context.index
            and controller._play_attempt == context.attempt
            and controller.current_item() is context.item
            and controller.mpv._process is context.process
            and context.process.poll() is None)


def _load_at(mpv, url, position, current):
    # The supported MPV loadfile schema takes file-local options after the index.
    # A reply only confirms that MPV accepted the playlist operation.
    if not mpv._pipe_lock.acquire(timeout=.02):
        return {"error": "busy"}
    try:
        if not current():
            return {"error": "stale"}
        return mpv._send_command("loadfile", [url, "replace", -1, {"start": str(position)}],
                                 timeout=.3, _locked=True)
    finally:
        mpv._pipe_lock.release()


def _progress(mpv, url, position, duration, deadline, current):
    last = None
    while current() and time.monotonic() < deadline:
        sample = read_sample(mpv.ipc_pipe, timeout=.15, expected_path=url)
        pos = number(sample.get("time-pos"))
        seen_duration = number(sample.get("duration"))
        size = sample.get("video-params") or {}
        if (pos is not None and position-.75 <= pos <= position+3
                and seen_duration is not None and abs(seen_duration-duration) < .75
                and size.get("w") and size.get("h") and sample.get("paused-for-cache") is False
                and sample.get("pause") is False and sample.get("seeking") is False):
            if last is not None and pos - last >= .15:
                return True
            if last is None:
                last = pos
        time.sleep(.05)
    return False


def handoff(controller, context, duration, permit):
    """Called under the existing media lock from an owned background task."""
    mpv = controller.mpv
    if not permit() or not context_current(controller, context):
        return "stale"
    sample = read_sample(mpv.ipc_pipe, timeout=.2, expected_path=context.original)
    position = number(sample.get("time-pos"))
    actual = number(sample.get("duration"))
    if (position is None or actual is None or abs(actual-duration) >= .75
            or not 0 <= position < duration-4 or sample.get("paused-for-cache") is not True
            or sample.get("seeking") is not False or sample.get("pause") is not False):
        return "stale"
    # A sole selected internal audio track follows the identical TS. External
    # tracks, subtitles and alternative audio choices keep the original session.
    if not mpv._pipe_lock.acquire(timeout=.02):
        return "rejected"
    try:
        reply = mpv._send_command("get_property", ["track-list"], timeout=.15, _locked=True)
    finally:
        mpv._pipe_lock.release()
    tracks = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(tracks, list) or any(t.get("external") or t.get("type") == "sub" for t in tracks):
        return "unsupported_tracks"
    audio = [t for t in tracks if t.get("type") == "audio"]
    if len(audio) > 1 or (audio and audio[0].get("selected") is not True):
        return "unsupported_tracks"
    if not permit() or not context_current(controller, context):
        return "stale"
    controller._quality_transition(suspend=True)
    revision = controller._switch_control_revision
    requested_at = time.monotonic()

    def current():
        return (context_current(controller, context)
                and controller._switch_control_revision == revision)

    # Suppress old-context progress observation from the first possible write.
    controller._switch_unsettled = True
    try:
        reply = _load_at(mpv, context.target, position, current)
    except (OSError, ValueError):
        return "unknown"
    if not isinstance(reply, dict):
        return "unknown"
    if reply.get("error") != "success":
        controller._switch_unsettled = False
        controller._start_playback_diagnostics("cms", context.item.get("_source"), media_url=context.original)
        return "rejected"

    if _progress(mpv, context.target, position, duration, time.monotonic()+3, current):
        if current():
            controller._current = context.alternative
            controller._playback_gen += 1
            controller._resume_pending = None
            controller._pending_history = None
            controller._film_last_sample = None
            controller._film_saved_at = 0
            controller._last_idle = False
            controller._switch_unsettled = False
            controller._start_playback_diagnostics("cms", context.alternative.get("_source"),
                                                   media_url=context.target, requested_at=requested_at)
            return "started"
    if not current():
        return "unknown" if context_current(controller, context) else "stale"
    # Check the candidate or an idle player via the separate read-only pipe.
    # Idle is accepted only when the path is absent at both ends of the batch.
    candidate = read_sample(mpv.ipc_pipe, timeout=.15, expected_path=context.target)
    idle = read_sample(mpv.ipc_pipe, timeout=.15, allow_idle=True, expected_path="")
    if (candidate or idle.get("idle-active") is True) and current():
        reply = _load_at(mpv, context.original, position, current)
        if isinstance(reply, dict) and reply.get("error") == "success":
            if _progress(mpv, context.original, position, duration, time.monotonic()+2, current):
                controller._switch_unsettled = False
                controller._start_playback_diagnostics("cms", context.item.get("_source"), media_url=context.original)
                return "restored"
            return "restore_requested"
    return "unknown"
