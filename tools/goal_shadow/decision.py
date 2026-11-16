"""One goal, evidence-first comparison. No command strings, dispatcher or I/O."""
from dataclasses import replace

from .contracts import Action, Availability as A, DecisionResult
from .providers import ChartProvider, CurrentSessionProvider, PersonalCollectionProvider

PROVIDERS = (CurrentSessionProvider(), PersonalCollectionProvider(), ChartProvider())


def decide(goal, world, *, now, current_generation, max_age=1.0):
    plans = tuple(p for provider in PROVIDERS for p in provider.propose(world, now))

    def result(action, reason, *, satisfied=False, expired=False, selected=""):
        return DecisionResult(action, reason, tuple(plans), world.generation, satisfied, expired,
                              selected_ref=selected)

    if (not world.consistent or current_generation != world.generation or
            not 0 <= now - world.captured_at <= max_age):
        return result(Action.NEEDS_CLARIFICATION, "snapshot_expired_or_inconsistent", expired=True)
    if goal.name != "LISTEN_MUSIC":
        return result(Action.NEEDS_CLARIFICATION, "unsupported_goal")
    constraints = goal.constraints
    if constraints.keep_content and constraints.change_content:
        return result(Action.NEEDS_CLARIFICATION, "conflicting_explicit_constraints")
    revised = []
    resources = {r.ref: r for r in world.resources}
    for p in plans:
        reasons = list(p.reasons)
        if constraints.keep_content and p.changes_content:
            reasons.append("explicit_keep_content")
        if constraints.change_content and (not p.changes_content or
                                           not p.content_ref or p.content_ref == world.current_ref):
            reasons.append("explicit_change_content")
        if p.resource_ref in constraints.excluded_refs:
            reasons.append("explicit_excluded_resource")
        if p.action in (Action.PLAY_PREFERRED, Action.PLAY_CHART) and not p.content_ref:
            reasons.append("specific_content_unproven")
        if p.preference[0] < 0:
            reasons.append("explicit_dislike")
        if constraints.required_tags:
            resource = resources.get(p.resource_ref)
            if resource is None or not constraints.required_tags <= resource.tags:
                reasons.append("required_tags_unproven")
        revised.append(replace(p, eligible=p.eligible and tuple(reasons) == p.reasons,
                               reasons=tuple(reasons)))
    plans = tuple(revised)
    valid = [p for p in plans if p.eligible]
    no_op = next((p for p in valid if p.action is Action.NO_OP), None)
    if no_op:
        return result(Action.NO_OP, "goal_already_observed_playing", satisfied=True, selected=no_op.resource_ref)
    if world.music_state == "unknown" or world.music.effective(now) is not A.YES:
        return result(Action.NEEDS_CLARIFICATION, "music_state_unproven")
    if world.movie_state not in ("stopped", "paused"):
        return result(Action.NEEDS_CLARIFICATION, "cross_media_policy_not_authorized")
    if valid:
        # Preserve session first; compare explicit preference before history/cost.
        valid.sort(key=lambda p: (p.changes_content, -p.preference[0], -p.preference[1], p.cost))
        winner = valid[0]
        ties = [p for p in valid if (p.changes_content, p.preference, p.cost) ==
                (winner.changes_content, winner.preference, winner.cost)]
        if len(ties) > 1:
            return result(Action.NEEDS_CLARIFICATION, "equivalent_personal_candidates")
        return result(winner.action, "fresh_capability_proof_minimum_change", selected=winner.resource_ref)
    if any(p.unknown for p in plans) or any(
            e is None or e.effective(now) is A.UNKNOWN for e in (world.inventory, world.catalog)):
        return result(Action.NEEDS_CLARIFICATION, "resources_exist_but_playability_unverified")
    return result(Action.UNAVAILABLE, "no_plan_satisfies_constraints")
