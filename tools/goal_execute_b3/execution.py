"""B1/B2 adapter + tightly scoped real execution, only on an owned Lab."""
from dataclasses import dataclass, field
import math
import time

from tools.goal_shadow.contracts import Goal
from tools.goal_shadow.readonly import off_gui_thread, queue_stamp, song_identity
from tools.goal_shadow_b2.capabilities import (
    ChartProvider, ChartRecord, PersonalCollectionProvider, source_capabilities,
)
from tools.goal_shadow_b2.decision import Action, Decision, Outcome, World, choose
from tools.goal_shadow_b2.sqlite_snapshot import ActiveReadSnapshot
from .lab import Lab


@dataclass(frozen=True)
class Observation:
    goal: Goal
    world: World
    decision: Decision
    token: tuple
    snapshot_ms: float
    decision_ms: float
    position: float | None
    props: dict = field(repr=False)


@dataclass(frozen=True)
class Result:
    status: Outcome
    accepted: bool
    goal_satisfied: bool
    attempts: int
    reason: str
    business_ms: float = 0
    confirmation_ms: float = 0
    user_satisfaction: str = "unobserved"


def _stamp(lab):
    ctrl = lab.ctrl
    if not ctrl._lock.acquire(timeout=.02):
        raise TimeoutError("controller_lock_busy")
    try:
        return (ctrl._play_request_id, ctrl._playback_gen, song_identity(ctrl._current_song),
                ctrl._paused_snapshot, ctrl._user_stopped, ctrl._is_switching_track,
                lab.manual_revision, lab.movie, queue_stamp(ctrl, time.monotonic())[0])
    finally:
        ctrl._lock.release()


def _file_stamps(lab):
    # Only explicitly generated files; no user path discovery or media decoding.
    result = []
    for path in sorted(lab.media.glob("*.wav"))[:256]:
        stat = path.stat()
        result.append((path.name, stat.st_ino, stat.st_size, stat.st_mtime_ns))
    return tuple(result)


def _state(props, song):
    position = props.get("time-pos")
    valid = type(position) in (int, float) and math.isfinite(position) and position >= 0
    if props.get("idle-active") is True:
        return "stopped", False
    if (props.get("idle-active") is not False or not valid or not song or
            props.get("path") != song.get("path")):
        return "unknown", False
    if props.get("pause") is True:
        return "paused", True
    after = props.get("time-pos-after")
    if props.get("pause") is False and type(after) in (int, float) and math.isfinite(after) and after > position:
        return "playing", True
    return "unknown", False


class Executor:
    def __init__(self, lab):
        if type(lab) is not Lab or not lab.active:
            raise TypeError("owned_active_lab_only")
        self.lab = lab
        self.reader = ActiveReadSnapshot(lab.root / "music.db", trusted_personal=True)
        self.last_confirmation = {}
        self.last_business_return = "not_called"

    def capture(self, goal=Goal()):
        off_gui_thread()
        lab = self.lab
        if not lab.active or goal.name != "LISTEN_MUSIC":
            raise ValueError("only_active_listen_music_lab")
        lab.movie = lab.observe_movie()
        before = _stamp(lab)
        props = lab.port.sample(time.monotonic() + .4)
        state, session = _state(props, dict(lab.ctrl._current_song or {}))
        snapshot = self.reader.read(budget=.15)
        now = time.monotonic()
        sources = source_capabilities(lab.ctrl.online_manager, now) if lab.ctrl.online_manager else ()
        caps = PersonalCollectionProvider().propose(snapshot, roots=[lab.media], now=now)
        if lab.source and lab.catalog_at:
            caps += ChartProvider().propose([ChartRecord("lab-chart", lab.source.name, lab.catalog_at)],
                sources=sources, service=lab.ctrl.recommendations, now=now)
        after = _stamp(lab)
        world = World(state, before[0], after[0] if before == after else -1, now,
                      before[2], session, lab.movie, snapshot.status, caps, goal.constraints)
        start = time.perf_counter_ns()
        decision = choose(world, time.monotonic())
        decision_ms = (time.perf_counter_ns() - start) / 1e6
        token = (after, tuple((c.ref, c.content_ref, c.level, c.epoch) for c in caps),
                 tuple((s.key, s.registered, s.epoch) for s in sources), _file_stamps(lab))
        return Observation(goal, world, decision, token, snapshot.elapsed_ms, decision_ms,
                           props.get("time-pos"), props)

    def _confirm(self, expected, request, revision, deadline, currency=lambda: True):
        started = time.perf_counter()
        while time.monotonic() < deadline:
            if (self.lab.manual_revision != revision or self.lab.ctrl._play_request_id != request
                    or not currency()):
                return Outcome.STALE, "request_changed_during_confirmation", (time.perf_counter()-started)*1000
            try:
                props = self.lab.port.sample(min(deadline, time.monotonic()+.3))
            except (TimeoutError, OSError, ValueError):
                # A bounded read ending without a property reply is unknown,
                # never proof of either playback or a side-effect-free failure.
                break
            state, _ = _state(props, {"path": expected})
            self.last_confirmation = dict(idle=props.get("idle-active"), paused=props.get("pause"),
                identity_matches=props.get("path") == expected,
                position_before=props.get("time-pos"), position_after=props.get("time-pos-after"))
            if state == "playing":
                if (not currency() or self.lab.manual_revision != revision
                        or self.lab.ctrl._play_request_id != request):
                    return Outcome.STALE, "currency_changed_during_read", (time.perf_counter()-started)*1000
                return Outcome.STARTED, "matching_path_and_progress", (time.perf_counter()-started)*1000
            time.sleep(.01)
        return Outcome.UNKNOWN, "accepted_or_effects_unconfirmed", (time.perf_counter()-started)*1000

    def execute(self, observed, *, budget=4, max_plans=2):
        off_gui_thread()
        lab = self.lab
        if not lab.active or observed.goal.name != "LISTEN_MUSIC":
            return Result(Outcome.STALE, False, False, 0, "lab_or_goal_invalid")
        deadline = time.monotonic()+min(max(0, budget), 5)
        if time.monotonic()-observed.world.captured_at > 1:
            return Result(Outcome.STALE, False, False, 0, "observation_expired")
        fresh = self.capture(observed.goal)
        if (fresh.token != observed.token or fresh.world.music != observed.world.music
                or fresh.decision != observed.decision):
            return Result(Outcome.STALE, False, False, 0, "state_resource_or_source_changed")
        if observed.decision.action is Action.NO_OP:
            return Result(Outcome.STARTED, False, True, 0, "already_playing_no_new_business")
        if observed.decision.action not in (Action.RESUME_MUSIC, Action.TRY_COLLECTION, Action.TRY_CHART):
            return Result(Outcome.UNKNOWN, False, False, 0, observed.decision.reason)
        revision = lab.manual_revision
        request = observed.world.generation
        source_token = observed.token[2]

        def current():
            # Can run while production code holds its own non-reentrant lock.
            return (lab.active and lab.manual_revision == revision and lab.movie == observed.world.movie
                    and time.monotonic() < deadline
                    and _file_stamps(lab) == observed.token[3]
                    and tuple((s.key, s.registered, s.epoch) for s in source_capabilities(
                        lab.ctrl.online_manager, time.monotonic())) == source_token)

        business_ms = 0
        for count, plan in enumerate(observed.decision.candidates[:min(max(max_plans, 0), 2)], 1):
            if not current() or lab.ctrl._play_request_id != request:
                return Result(Outcome.STALE, False, False, count-1, "currency_lost", business_ms)
            expected, value = None, None
            load_start = len(lab.mpv.loads)
            pause_start = len(lab.mpv.pause_commands)
            start = time.perf_counter()
            from app.core.command_bus import Command, CommandType
            if plan.action is Action.RESUME_MUSIC:
                expected = fresh.props.get("path")
                value = lab.bus.execute(Command(CommandType.MUSIC_RESUME,
                    is_current=lambda: current() and lab.ctrl._play_request_id == request,
                    source="b3", notification_id=""))
            elif plan.action is Action.TRY_COLLECTION:
                if lab.refuse_collection_once:
                    # A deliberately injected admission failure, not a fake MPV
                    # receipt. No business method/queue/player has been touched.
                    lab.refuse_collection_once = False
                    continue
                # There is no production guarded play_playlist_item API. The
                # lab gate serializes ONLY this lab's explicit manual operations.
                if not lab.action_gate.acquire(timeout=.02):
                    return Result(Outcome.STALE, False, False, count-1, "lab_action_busy", business_ms)
                try:
                    checked = self.capture(observed.goal)
                    live = next((c for c in checked.world.capabilities if c.ref == plan.ref
                                 and c.content_ref == plan.content_ref and c.epoch == plan.epoch), None)
                    if not live or checked.token != observed.token or not current():
                        return Result(Outcome.STALE, False, False, count-1, "membership_or_identity_changed", business_ms)
                    item = lab.members[plan.ref]
                    song = lab.ctrl.db.get_song_by_id(item["music_id"])
                    expected = song.get("path") if song else None
                    value = lab.ctrl.play_playlist_item(item)
                finally:
                    lab.action_gate.release()
            elif plan.action is Action.TRY_CHART:
                value = lab.bus.execute(Command(CommandType.MUSIC_RECOMMEND,
                    params={"expected_request": request}, is_current=current,
                    source="b3", notification_id=""))
                expected = (lab.ctrl.get_current_song() or {}).get("path")
            else:
                return Result(Outcome.UNKNOWN, False, False, count-1, "action_not_allowlisted", business_ms)
            business_ms += (time.perf_counter()-start)*1000
            self.last_business_return = (str(value.status.value) if hasattr(value, "status") else
                                         "none" if value is None else "true" if value is True else
                                         "false" if value is False else "other")
            if lab.manual_revision != revision or not current():
                expired = time.monotonic() >= deadline
                return Result(Outcome.UNKNOWN if expired else Outcome.STALE, False, False, count,
                              "total_budget_expired" if expired else "request_or_source_changed", business_ms)
            loads = lab.mpv.loads[load_start:]
            sent = any(row["sent"] for row in loads)
            acknowledged = any(row["ack"] == "success" for row in loads)
            if plan.action is Action.RESUME_MUSIC:
                acknowledged = any(paused is False and ack == "success"
                                   for paused, ack in lab.mpv.pause_commands[pause_start:])
            # A pre-IPC refusal may safely fall back. Decoder failures after
            # accepted loadfile are NOT considered side-effect-free failures.
            if loads and not sent:
                return Result(Outcome.UNKNOWN, False, False, count,
                              "legacy_queue_effects_prevent_safe_fallback", business_ms)
            if expected is not None and (sent or plan.action is Action.RESUME_MUSIC):
                status, reason, confirmation = self._confirm(expected, lab.ctrl._play_request_id,
                    revision, min(deadline, time.monotonic()+.45), current)
                if status is Outcome.STARTED:
                    return Result(status, acknowledged, True, count, reason, business_ms, confirmation)
                # Explicit idle after a known corrupt load is a failed attempt,
                # but the old queue can have changed: do not auto-fallback.
                try:
                    idle = lab.port.get_property("idle-active", min(deadline, time.monotonic()+.2))
                except (TimeoutError, OSError, ValueError):
                    idle = None
                if status is not Outcome.STALE and idle is True and sent:
                    return Result(Outcome.FAILED, acknowledged, False, count,
                                  "decoder_idle_after_load_no_fallback", business_ms, confirmation)
                return Result(status, acknowledged, False, count, reason, business_ms, confirmation)
            if not sent and value is False:
                return Result(Outcome.FAILED, False, False, count, "business_rejected", business_ms)
            return Result(Outcome.UNKNOWN, acknowledged, False, count, "completion_or_side_effects_unknown", business_ms)
        return Result(Outcome.FAILED, False, False, min(len(observed.decision.candidates), max_plans, 2),
                      "bounded_plans_exhausted", business_ms)
