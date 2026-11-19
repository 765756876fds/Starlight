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
    line_binding: tuple | None = None


def _feature_episode(episodes):
    return len(episodes) == 1 and episodes[0][0].casefold() in {"正片", "全片", "hd", "movie"}


def _film_url(item):
    kind = str(item.get("type_name") or item.get("vod_type") or "").strip().casefold()
    if kind not in {"电影", "movie", "动作片", "喜剧片", "科幻片", "剧情片", "爱情片",
                    "恐怖片", "战争片", "纪录片", "动画电影", "悬疑片", "犯罪片"}:
        return None
    episodes = episode_eps(item)
    if not _feature_episode(episodes):
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


def _line_binding(item):
    return (item.get("_source_id"), item.get("vod_id"), item.get("vod_play_url"),
            item.get("_playback_line_index"), item.get("play_url"), item.get("first_url"))


def _other_line(current, original):
    """Use only the raw lines of the loaded CMS record. Line labels carry no identity."""
    raw = current.get("vod_play_url")
    record = current.get("vod_id")
    selected = current.get("_playback_line_index")
    if (type(record) not in (int, str) or not str(record).strip() or not record
            or type(selected) is not int or not isinstance(raw, str) or len(raw) > 32768):
        return None
    parts = raw.split("$$$")
    if not 2 <= len(parts) <= 16 or not 0 <= selected < len(parts):
        return None
    if (current.get("play_url") != parts[selected] or "#" in parts[selected]
            or current.get("first_url") != original):
        return None
    for index, line in enumerate(parts):
        if index == selected or "#" in line:
            continue
        # Validate an individual line before asking the CMS parser to select it.
        target = _film_url(dict(current, play_url=line))
        if target and origin(target) != origin(original):
            alternative = normalise_playback(current, index)
            if alternative.get("first_url") == target:
                return alternative
    return None


def assess_context(controller, group):
    current = controller.current_item()
    if not current or not current.get("_source_id"):
        return None, "identity_insufficient"
    if controller._current_index != 0 or not _feature_episode(episode_eps(current)):
        return None, "episode_mismatch"
    original = _film_url(current)
    process = getattr(controller.mpv, "_process", None)
    if not original or process is None:
        return None, "unsupported_hls"
    alternative = _other_line(current, original)
    if alternative is not None:
        return SwitchContext(controller._playback_gen, 0, controller._play_attempt, process,
                             current, original, alternative, alternative["first_url"],
                             _line_binding(current)), None
    work = work_identity(current)
    if work is None:
        return None, "identity_insufficient"
    # Multi-line selection belongs only to the authoritative loaded record.
    if len(current.get("_playback_lines") or ()) > 1:
        return None, "no_candidate"
    reason = "no_candidate"
    for row in group:
        if not row.get("_source_id") or row["_source_id"] == current["_source_id"]:
            continue
        if work_identity(row) != work:
            reason = "identity_insufficient"
            continue
        alternative = normalise_playback(row)
        if len(alternative.get("_playback_lines") or ()) > 1:
            reason = "unsupported_hls"
            continue
        target = _film_url(alternative)
        if not target:
            reason = "episode_mismatch" if not _feature_episode(episode_eps(alternative)) else "unsupported_hls"
            continue
        if target and origin(original) != origin(target):
            return SwitchContext(generation=controller._playback_gen, index=0,
                                 attempt=controller._play_attempt, process=process,
                                 item=current, original=original, alternative=alternative, target=target), None
    return None, reason


def context_for(controller, group):
    return assess_context(controller, group)[0]


def record_master(context):
    """Consider at most one publisher index from the selected CMS record."""
    if context.line_binding is None:
        return None
    for line in context.line_binding[2].split("$$$"):
        url = _film_url(dict(context.item, play_url=line))
        if url and url != context.original and origin(url) == origin(context.original):
            return url
    return None


def failed_start_current(controller, context):
    receipt = getattr(controller, "_failed_switch_start", None)
    return bool(isinstance(receipt, tuple) and context and context.line_binding and
                receipt[:4] == (context.generation, context.attempt, context.index, id(context.item))
                and receipt[4] == controller._switch_control_revision and time.monotonic() < receipt[5]
                and controller.current_item() is None and controller.playback_phase() == "failed"
                and controller._playback_gen == context.generation and controller._play_attempt == context.attempt
                and controller.mpv._process is context.process and context.process.poll() is None
                and not getattr(controller, "_media_voice_closed", False)
                and _line_binding(context.item) == context.line_binding)


def context_current(controller, context):
    return (not getattr(controller, "_media_voice_closed", False)
            and controller._playback_gen == context.generation
            and controller._current_index == context.index
            and controller._play_attempt == context.attempt
            and controller.current_item() is context.item
            and (context.line_binding is None or _line_binding(context.item) == context.line_binding)
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


def _progress(mpv, url, position, duration, deadline, current, *, entry=None):
    last = None
    while current() and time.monotonic() < deadline:
        options = {"expected_entry": entry} if entry is not None else {}
        sample = read_sample(mpv.ipc_pipe, timeout=.15, expected_path=url, **options)
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


def _track_rejection(mpv):
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
    return None


def restart_line(controller, context, duration, permit):
    """One explicit load from zero. Unknown writes never trigger rollback or retry."""
    if not context.line_binding or not permit() or getattr(controller, "_switch_unsettled", False):
        return "stale"
    mpv = controller.mpv
    from_failure = failed_start_current(controller, context)
    if from_failure:
        idle = read_sample(mpv.ipc_pipe, timeout=.2, expected_path="", allow_idle=True)
        if idle.get("idle-active") is not True:
            return "stale"
    else:
        if not context_current(controller, context):
            return "stale"
        session = getattr(controller, "_film_session", None) or {}
        sample = read_sample(mpv.ipc_pipe, timeout=.2, expected_path=context.original,
                             expected_entry=session.get("entry"))
        if (sample.get("paused-for-cache") is not True or sample.get("pause") is not False
                or sample.get("seeking") is not False):
            return "rejected"
        rejection = _track_rejection(mpv)
        if rejection:
            return rejection
        controller._remember_view(False, force=True, sample=sample)

    revision = controller._switch_control_revision

    def current():
        owned = failed_start_current if from_failure else context_current
        return controller._switch_control_revision == revision and owned(controller, context)

    if not current() or not permit():
        return "stale"
    requested_at = time.monotonic()
    controller._switch_unsettled = True
    reply = _load_at(mpv, context.target, 0, lambda: current() and permit())
    if isinstance(reply, dict) and reply.get("error") in ("busy", "stale"):
        controller._switch_unsettled = False
        return "rejected"
    if not isinstance(reply, dict) or reply.get("error") != "success":
        return "unknown"
    data = reply.get("data")
    entry = data.get("playlist_entry_id") if isinstance(data, dict) else None
    if type(entry) is not int:
        return "unknown"
    if _progress(mpv, context.target, 0, duration, time.monotonic()+3, current, entry=entry) and current():
        controller._quality_transition(suspend=True)
        controller._current = context.alternative
        controller._current_index = context.index
        controller._accept_episode(reply, context.target)
        controller._film_session["advanced"] = True
        controller._film_phase = "playing"
        controller._last_idle = False
        controller._switch_unsettled = False
        if from_failure:
            controller._record_history_if_started(False)
        else:
            controller._pending_history = None
        controller._start_playback_diagnostics("cms", context.alternative.get("_source"),
                                               media_url=context.target, requested_at=requested_at)
        controller._write_state(playing=True, paused=False)
        return "started"
    if current() and mpv.film_end_reason(entry) == "error":
        controller._finish_film_session("failed")
        controller._switch_unsettled = False
        return "restart_failed"
    return "unknown"


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
            controller._begin_film_session(reply, context.target)
            controller._film_session["advanced"] = True
            controller._film_phase = "playing"
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
                controller._begin_film_session(reply, context.original)
                controller._film_session["advanced"] = True
                controller._film_phase = "playing"
                controller._start_playback_diagnostics("cms", context.item.get("_source"), media_url=context.original)
                return "restored"
            return "restore_requested"
    return "unknown"
