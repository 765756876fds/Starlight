"""Pure B2 comparison and synthetic-only bounded outcomes, not a player adapter."""
from dataclasses import dataclass
from enum import Enum
import time

from tools.goal_shadow.contracts import Availability as A, GoalConstraints
from .capabilities import Capability, Level


class Action(str, Enum):
    NO_OP = "NO_OP"
    RESUME_MUSIC = "RESUME_MUSIC"
    TRY_COLLECTION = "TRY_COLLECTION"
    TRY_CHART = "TRY_CHART"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
    UNAVAILABLE = "UNAVAILABLE"


class Outcome(str, Enum):
    STARTED = "STARTED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class World:
    music: str
    generation: int
    snapshot_generation: int
    captured_at: float
    current_ref: str = ""
    session_valid: bool = False
    movie: str = "unknown"
    inventory: str = "error"
    capabilities: tuple[Capability,...] = ()
    constraints: GoalConstraints = GoalConstraints()


@dataclass(frozen=True)
class Plan:
    action: Action
    ref: str
    epoch: tuple
    content_ref: str
    reason: tuple[str,...]
    preparation_required: bool = False


@dataclass(frozen=True)
class Decision:
    action: Action
    reason: str
    candidates: tuple[Plan,...] = ()
    rejected: tuple[tuple[str,str],...] = ()
    goal_satisfied: bool = False

    def legacy_status(self):
        return "NEEDS_CLARIFICATION" if self.action is Action.NEEDS_VERIFICATION else self.action.value


def choose(world, now):
    constraints = world.constraints
    if constraints.keep_content and constraints.change_content:
        return Decision(Action.NEEDS_CLARIFICATION,"conflicting_constraints")
    if world.generation != world.snapshot_generation or not 0 <= now-world.captured_at <= 1:
        return Decision(Action.NEEDS_VERIFICATION,"snapshot_stale")
    if (world.music == "playing" and world.current_ref and not constraints.change_content
            and not constraints.required_tags and world.current_ref not in constraints.excluded_refs):
        return Decision(Action.NO_OP,"goal_already_observed_playing",goal_satisfied=True)
    if world.music == "unknown":
        return Decision(Action.NEEDS_CLARIFICATION,"player_state_unknown")
    if world.movie not in ("paused","stopped"):
        return Decision(Action.NEEDS_CLARIFICATION,"cross_media_policy_not_authorized")
    plans, rejected = [], []
    if (world.music == "paused" and world.session_valid and world.current_ref and not constraints.change_content
            and not constraints.required_tags and world.current_ref not in constraints.excluded_refs):
        plans.append(Plan(Action.RESUME_MUSIC,world.current_ref,(),world.current_ref,("same_reliable_paused_session",)))
    eligible, unverified = [], False
    for c in world.capabilities:
        reason = ""
        if c.provider not in ("collection","chart"):
            reason = "unsupported_capability_provider"
            unverified = True
        elif constraints.keep_content or constraints.change_content and c.content_ref == world.current_ref:
            reason = "explicit_content_constraint"
        elif c.ref in constraints.excluded_refs or c.explicit_preference < 0:
            reason = "explicit_exclusion"
        elif constraints.required_tags:
            reason = "tag_evidence_missing"
            unverified = True
        elif not c.fresh(now) or c.level is Level.UNVERIFIED or c.exists is not A.YES:
            reason = "capability_unverified_or_expired"
            unverified = True
        elif c.provider == "collection" and c.owned is not A.YES:
            reason = "ownership_unproven"
            unverified = True
        elif c.level is Level.UNAVAILABLE:
            reason = "capability_unavailable"
        elif c.level in (Level.ATTEMPTABLE,Level.PLAYBACK_CONFIRMED):
            eligible.append(c)
        if reason:
            rejected.append((c.ref,reason))
    eligible.sort(key=lambda c:(-c.explicit_preference,-c.history,c.provider!="collection",c.ref))
    for c in eligible:
        if c.provider == "chart" and any(p.action is Action.TRY_CHART for p in plans):
            rejected.append((c.ref,"existing_recommender_macro_already_present"))
            continue
        plans.append(Plan(Action.TRY_COLLECTION if c.provider=="collection" else Action.TRY_CHART,
                          c.ref,c.epoch,c.content_ref,c.basis,c.preparation_required))
    if plans:
        return Decision(plans[0].action,"bounded_attempt_not_playback_confirmation",tuple(plans),tuple(rejected))
    if unverified or world.inventory in ("error","partial","unverified") or world.music == "paused":
        return Decision(Action.NEEDS_VERIFICATION,"read_or_attempt_evidence_missing",rejected=tuple(rejected))
    return Decision(Action.UNAVAILABLE,"no_admissible_plan",rejected=tuple(rejected))


@dataclass(frozen=True)
class Receipt:
    status: Outcome
    accepted: bool = False
    no_effect_confirmed: bool = False
    progressed: bool = False
    generation_current: bool = True
    resource_current: bool = True


@dataclass(frozen=True)
class Simulation:
    status: Outcome
    attempts: int
    accepted_attempts: int
    goal_satisfied: bool
    reason: str
    executed_real_business: bool = False
    user_satisfaction: str = "unobserved"


class SyntheticExecutor:
    """Only a finite list of synthetic receipts; cannot accept a callback/API."""
    def __init__(self, receipts):
        self.receipts = tuple(receipts)
        self.calls = 0

    def attempt(self, plan, budget):
        if budget <= 0:
            return Receipt(Outcome.TIMEOUT,no_effect_confirmed=True)
        index = self.calls
        self.calls += 1
        return self.receipts[index] if index < len(self.receipts) else Receipt(Outcome.UNKNOWN)


def simulate(decision, executor, *, max_attempts=3, budget=.2):
    if type(executor) is not SyntheticExecutor:
        raise TypeError("synthetic_executor_only")
    if decision.action is Action.NO_OP:
        return Simulation(Outcome.STARTED,0,0,True,"goal_previously_observed_no_new_attempt")
    deadline = time.monotonic()+min(max(budget,0),1)
    attempts, accepted = 0,0
    last_failure = Outcome.FAILED
    for plan in decision.candidates[:min(max(max_attempts,0),3)]:
        if time.monotonic() >= deadline:
            return Simulation(Outcome.TIMEOUT,attempts,accepted,False,"total_budget_exhausted")
        receipt = executor.attempt(plan,deadline-time.monotonic())
        attempts += 1
        accepted += int(receipt.accepted)
        if not receipt.generation_current or not receipt.resource_current or receipt.status is Outcome.STALE:
            return Simulation(Outcome.STALE,attempts,accepted,False,"request_or_resource_changed")
        if receipt.status is Outcome.STARTED:
            if receipt.accepted and receipt.progressed:
                return Simulation(Outcome.STARTED,attempts,accepted,True,"synthetic_progress_confirmation")
            return Simulation(Outcome.UNKNOWN,attempts,accepted,False,"started_claim_without_progress")
        if receipt.status in (Outcome.FAILED,Outcome.TIMEOUT) and receipt.no_effect_confirmed:
            last_failure = receipt.status
            continue
        # Never issue another plan while an accepted/ambiguous load might play.
        return Simulation(receipt.status if receipt.status is Outcome.TIMEOUT else Outcome.UNKNOWN,
                          attempts,accepted,False,"side_effects_or_completion_unconfirmed")
    if attempts:
        return Simulation(last_failure,attempts,accepted,False,"bounded_candidates_exhausted")
    return Simulation(Outcome.UNKNOWN,0,0,False,"no_admissible_attempt")
