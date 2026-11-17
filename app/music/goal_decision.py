"""Opt-in, pure LISTEN_MUSIC comparison. No player, SQL or recommendation calls."""

from dataclasses import dataclass

from .goal_admission import MemberToken, StateToken


@dataclass(frozen=True)
class Candidate:
    plan: str
    availability: str  # attemptable / unavailable / unknown
    member: MemberToken | None = None
    explicit_match: bool = False
    excluded: bool = False
    reason: str = ""


@dataclass(frozen=True)
class Decision:
    plan: str | None
    reason: str
    member: MemberToken | None = None
    status: str = "prepared"


def decide(state: StateToken, movie_playing, candidates, *, now, goal="LISTEN_MUSIC"):
    if goal != "LISTEN_MUSIC":
        return Decision(None, "only_unconstrained_listen_music", status="rejected_unavailable")
    if not 0 <= now - state.captured <= 1:
        return Decision(None, "snapshot_expired", status="rejected_stale")
    session = bool(state.content) and state.entry is not None and not state.stopped
    if state.media_state == "playing" and session:
        return Decision("NO_OP", "keep_session_pending_progress_proof")
    if state.media_state == "unknown":
        return Decision(None, "music_state_unknown", status="needs_verification")
    if movie_playing is not False:
        return Decision(None, "cross_media_permission_unknown", status="needs_clarification")
    if state.media_state == "paused":
        if session:
            return Decision("RESUME_MUSIC", "same_reliable_paused_session")
        return Decision(None, "paused_session_unproven", status="needs_verification")
    if state.media_state != "stopped":
        return Decision(None, "session_unproven", status="needs_verification")
    valid = [c for c in candidates if c.availability == "attemptable" and not c.excluded]
    local = next((c for c in valid if c.plan == "TRY_COLLECTION" and c.explicit_match), None)
    chart = next((c for c in valid if c.plan == "TRY_CHART"), None)
    if local and (chart is None or not chart.explicit_match):
        return Decision(local.plan, "verified_local_explicit_preference", local.member)
    if chart:
        return Decision(chart.plan, "keep_existing_chart_recommendation")
    local = next((c for c in valid if c.plan == "TRY_COLLECTION"), None)
    if local:
        return Decision(local.plan, "local_attempt_available_chart_not_attemptable", local.member)
    unknown = any(c.availability == "unknown" for c in candidates)
    return Decision(
        None,
        "resource_evidence_missing" if unknown else "no_legal_candidate",
        status="needs_verification" if unknown else "unavailable",
    )
