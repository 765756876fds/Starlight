"""Best-effort collection lifecycle counters. No titles, paths, URLs or raw IDs."""
import hashlib

from app.core.logger import get_logger

STAGES = {"chart_read", "established", "loaded", "request_cancelled", "session_cleared", "cleared", "replaced", "next"}
REASONS = {"recommendation", "direct_online", "online_list", "cancelled", "shutdown",
           "clear_queue", "play_song", "play_search_result", "stop_current", "preference",
           "manual", "natural", "next", "prev", "pause", "resume", "toggle_pause"}
MODES = {"sequential", "shuffle", "repeat_one", "repeat_all"}


def record_collection(controller, stage, reason, *, source=None, chart_id=None,
                      track_count=None, retained_count=None):
    try:
        snapshot = controller.queue.diagnostic_snapshot()
        def reference(value):
            return hashlib.sha256(str(value).encode("utf8")).hexdigest()[:12] if value is not None else "none"
        service = getattr(controller, "recommendations", None)
        get_logger().info(
            "music_collection stage=%s reason=%s source_ref=%s chart_ref=%s "
            "track_count=%s retained_count=%s queue_size=%s index=%s mode=%s session_active=%s request_id=%s",
            stage if stage in STAGES else "unknown", reason if reason in REASONS else "unknown",
            reference(source), reference((source, chart_id)) if chart_id is not None else "none",
            track_count if type(track_count) is int else "unknown",
            retained_count if type(retained_count) is int else "unknown",
            snapshot["size"], snapshot["index"],
            snapshot["mode"] if snapshot["mode"] in MODES else "unknown",
            bool(getattr(service, "session", None)), controller._play_request_id)
    except Exception:
        pass  # Diagnostics never change playback, queue state or error recovery.
