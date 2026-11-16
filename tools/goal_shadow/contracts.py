"""Read-only LISTEN_MUSIC contracts; a decision never executes an action."""
from dataclasses import dataclass
from enum import Enum


class Availability(str, Enum):
    YES = "available"
    NO = "unavailable"
    UNKNOWN = "unverified"


class Action(str, Enum):
    NO_OP = "NO_OP"
    RESUME_MUSIC = "RESUME_MUSIC"
    PLAY_PREFERRED = "PLAY_PREFERRED"
    PLAY_CHART = "PLAY_CHART"
    UNAVAILABLE = "UNAVAILABLE"
    NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"


@dataclass(frozen=True)
class GoalConstraints:
    keep_content: bool = False
    change_content: bool = False
    required_tags: frozenset[str] = frozenset()
    excluded_refs: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Goal:
    name: str = "LISTEN_MUSIC"
    constraints: GoalConstraints = GoalConstraints()


@dataclass(frozen=True)
class CapabilityEvidence:
    state: Availability
    source: str
    observed_at: float
    reliable: bool = False
    ttl: float = 2.0
    reason: str = ""

    def effective(self, now):
        if not self.reliable or not 0 <= now - self.observed_at <= self.ttl:
            return Availability.UNKNOWN
        return self.state


@dataclass(frozen=True)
class Resource:
    ref: str  # Opaque identity only. No title/path/URL is persisted.
    kind: str
    exists: CapabilityEvidence
    playable: CapabilityEvidence
    owned: CapabilityEvidence
    count: int = 0
    explicit_preference: float = 0.0
    historical_preference: float = 0.0
    tags: frozenset[str] = frozenset()
    content_ref: str = ""
    artist_ref: str = ""


@dataclass(frozen=True)
class WorldStateSnapshot:
    captured_at: float
    finished_at: float
    generation: tuple
    music_state: str
    music: CapabilityEvidence
    session: CapabilityEvidence
    current_ref: str = ""
    queue: CapabilityEvidence | None = None
    queue_revision: int | None = None
    resources: tuple[Resource, ...] = ()
    movie_state: str = "unknown"
    consistent: bool = False
    notes: tuple[str, ...] = ()
    inventory: CapabilityEvidence | None = None
    catalog: CapabilityEvidence | None = None


@dataclass(frozen=True)
class CandidatePlan:
    action: Action
    resource_ref: str
    preconditions: tuple[CapabilityEvidence, ...]
    goal_conditions: tuple[str, ...]
    resource_source: str
    changes_content: bool
    changes_queue: bool
    cost: int
    preference: tuple[float, float]
    eligible: bool
    unknown: bool
    reasons: tuple[str, ...]
    content_ref: str = ""


@dataclass(frozen=True)
class DecisionResult:
    action: Action
    reason: str
    candidates: tuple[CandidatePlan, ...]
    generation: tuple
    goal_satisfied: bool = False
    expired: bool = False
    execution_performed: bool = False
    selected_ref: str = ""
