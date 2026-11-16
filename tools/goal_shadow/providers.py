"""Providers describe capabilities, never choose the winner or run business code."""
from dataclasses import replace

from .contracts import Action, Availability as A, CandidatePlan, CapabilityEvidence


def candidate(action, resource, evidence, now, *, cost, changes, preference=(0, 0)):
    states = [e.effective(now) for e in evidence]
    unknown = A.UNKNOWN in states
    return CandidatePlan(
        action, resource, tuple(evidence), ("music_observed_playing",),
        ";".join(e.source for e in evidence), changes, changes, cost, preference,
        all(s is A.YES for s in states), unknown,
        tuple(e.reason or e.source + ":" + s.value for e, s in zip(evidence, states)),
    )


class CurrentSessionProvider:
    def propose(self, world, now):
        if world.music_state == "playing":
            return (candidate(Action.NO_OP, world.current_ref, (world.music,), now,
                              cost=0, changes=False),)
        if world.music_state == "paused":
            return (candidate(Action.RESUME_MUSIC, world.current_ref,
                              (world.music, world.session), now, cost=1, changes=False),)
        return ()


class PersonalCollectionProvider:
    def propose(self, world, now):
        return tuple(replace(candidate(Action.PLAY_PREFERRED, row.ref,
                               (row.exists, row.owned, row.playable), now,
                               cost=3, changes=True,
                               preference=(row.explicit_preference, row.historical_preference)),
                             content_ref=row.content_ref)
                     for row in world.resources if row.kind == "collection")


class ChartProvider:
    def propose(self, world, now):
        return tuple(replace(candidate(Action.PLAY_CHART, row.ref, (row.exists, row.playable),
                               now, cost=4, changes=True,
                               preference=(row.explicit_preference, row.historical_preference)),
                             content_ref=row.content_ref)
                     for row in world.resources if row.kind == "chart")


def unavailable_evidence(source, now, reason):
    return CapabilityEvidence(A.UNKNOWN, source, now, reason=reason)
