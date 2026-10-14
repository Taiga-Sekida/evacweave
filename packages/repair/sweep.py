"""Every group of residents under the reviewer's restrictions: which ones a plan can serve.

Rule-only and deterministic: no model is called. For each pattern the restrictions go
into the scenario, the scenario's own plan is checked, and if the Simulation Core rejects
it the bounded repair search looks for one it accepts. A pattern without such a plan gets
a cause. Then changes to one shelter are tried: meeting one more need there, or a few
more places. Each single change is scored by the failed patterns it rescues, and a short
sequence is built by taking, each time, the change that rescues the most patterns left.
A PASS here is a property of synthetic data and of this search; it says nothing about
real shelters.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from itertools import combinations

from packages.domain.care import (
    Avoids,
    CareSweep,
    PatternCheck,
    SweepCause,
    SweepChange,
    SweepFix,
    SweepStep,
    one_per_pair,
)
from packages.domain.models import Plan, RestrictionReason, Scenario
from packages.repair.planner import SearchBudgetExceeded, propose_repair
from packages.simulation.engine import simulate

CAPACITY_STEP = 2
MAX_FIXES = 6
MAX_STEPS = 3

# (shelter id, change, reason for meet_need or None). Kept a plain tuple: changes are
# ordered by str(change), which a named tuple's repr would change.
Change = tuple[str, SweepChange, RestrictionReason | None]
# Which of the given patterns (by position) verify once the given changes are made.
RescuedBy = Callable[[Sequence[Change], Sequence[int]], list[int]]


def _has_verified_plan(scenario: Scenario, plan: Plan) -> bool:
    if simulate(scenario, plan).status == "PASS":
        return True
    try:
        return propose_repair(scenario, plan) is not None
    except SearchBudgetExceeded:
        return False


def _world(scenario: Scenario, avoids: Avoids, changes: Sequence[Change] = ()) -> Scenario:
    """The scenario with a pattern's restrictions, after some shelters were changed.

    Meeting a need lifts that reason only: a resident the shelter fails in another way
    is still kept out.
    """
    met = {(shelter, reason) for shelter, change, reason in changes if change == "meet_need"}
    extra = Counter(shelter for shelter, change, _ in changes if change == "add_capacity")
    kept = tuple(a for a in avoids if (a.shelter_id, a.reason) not in met)
    shelters = tuple(
        s.model_copy(update={"capacity": s.capacity + CAPACITY_STEP * extra[s.id]}) if extra[s.id] else s
        for s in scenario.shelters
    )
    return Scenario.model_validate(
        scenario.model_copy(update={"shelters": shelters, "restrictions": one_per_pair(kept)})
    )


Cause = tuple[SweepCause, tuple[str, ...], tuple[str, ...]]  # cause, residents, shelters


def _cause(scenario: Scenario) -> Cause:
    """Why no plan was found, from who may go where (Hall's condition on capacities)."""
    open_shelters = [s for s in scenario.shelters if s.capacity > 0]
    barred: dict[str, set[str]] = defaultdict(set)
    for r in scenario.restrictions:
        barred[r.actor_id].add(r.shelter_id)
    allowed = {p.id: {s.id for s in open_shelters} - barred[p.id] for p in scenario.personas}
    stranded = tuple(sorted(actor for actor, shelters in allowed.items() if not shelters))
    if stranded:
        return "no_shelter", stranded, ()
    capacity = {s.id: s.capacity for s in open_shelters}
    ids = sorted(capacity)
    for size in range(1, len(ids) + 1):
        for group in combinations(ids, size):
            only_here = [a for a, shelters in allowed.items() if shelters <= set(group)]
            if len(only_here) > sum(capacity[s] for s in group):
                return "capacity", tuple(sorted(only_here)), group
    return "route", (), ()


def _most_rescued_first(item: tuple[Change, list[int]]) -> tuple[int, str, str, str]:
    (shelter, change, reason), rescued = item
    return (-len(rescued), change, shelter, reason or "")


def _check_patterns(scenario: Scenario, plan: Plan, patterns: Sequence[Avoids]) -> list[PatternCheck]:
    """Each pattern under its restrictions, as it is; the cause where no plan verifies."""
    checks: list[PatternCheck] = []
    for index, avoids in enumerate(patterns):
        world = _world(scenario, avoids)
        needs, restrictions = len({r.actor_id for r in avoids}), len(world.restrictions)
        if _has_verified_plan(world, plan):
            checks.append(
                PatternCheck(
                    pattern=index, residents_with_needs=needs, restrictions=restrictions, verified=True
                )
            )
            continue
        cause, residents, shelters = _cause(world)
        checks.append(
            PatternCheck(
                pattern=index,
                residents_with_needs=needs,
                restrictions=restrictions,
                verified=False,
                cause=cause,
                residents=residents,
                shelters=shelters,
            )
        )
    return checks


def _candidate_changes(scenario: Scenario, patterns: Sequence[Avoids], failed: list[int]) -> set[Change]:
    """More places at any shelter, or meeting a need a failed pattern was kept out for."""
    candidates: set[Change] = {(s.id, "add_capacity", None) for s in scenario.shelters}
    for index in failed:
        # location_privacy is about who may be at or near the shelter (someone the resident
        # must be hidden from, such as an abuser living nearby), not a facility it lacks: no
        # change to the shelter meets it, so it is never offered as a fix.
        candidates.update(
            (a.shelter_id, "meet_need", a.reason) for a in patterns[index] if a.reason != "location_privacy"
        )
    return candidates


def _single_fixes(candidates: set[Change], failed: list[int], rescued_by: RescuedBy) -> tuple[SweepFix, ...]:
    """Each change on its own, most rescued first: the first MAX_FIXES that rescue any."""
    scored = [(change, rescued_by([change], failed)) for change in sorted(candidates, key=str)]
    best_first = sorted(scored, key=_most_rescued_first)
    return tuple(
        SweepFix(shelter_id=shelter, change=change, reason=reason, rescued=tuple(rescued))
        for (shelter, change, reason), rescued in best_first[:MAX_FIXES]
        if rescued
    )


def _greedy_steps(
    candidates: set[Change], failed: list[int], pattern_count: int, rescued_by: RescuedBy
) -> tuple[SweepStep, ...]:
    """Changes made one after another, each the one that rescues the most patterns left."""
    steps: list[SweepStep] = []
    made: list[Change] = []
    left = list(failed)
    verified_now = pattern_count - len(failed)
    for _ in range(MAX_STEPS):
        options = [(c, rescued_by([*made, c], left)) for c in sorted(candidates - set(made), key=str)]
        if not options:
            break
        best, rescued = min(options, key=_most_rescued_first)
        if not rescued:
            break
        # Recount every pattern with all the changes so far, not only the ones left:
        # the step's number is what the Core says, not a sum of earlier answers.
        served = rescued_by([*made, best], range(pattern_count))
        if len(served) <= verified_now:
            break
        made.append(best)
        left = [i for i in range(pattern_count) if i not in served]
        verified_now = len(served)
        shelter, change, reason = best
        steps.append(SweepStep(shelter_id=shelter, change=change, reason=reason, verified=verified_now))
    return tuple(steps)


def sweep(scenario: Scenario, plan: Plan, patterns: Sequence[Avoids]) -> CareSweep:
    """Check each pattern (by position), then score changes on the ones that failed.

    A pattern is given as every reason the reviewer gave (pattern_avoids), so that
    meeting one need is not mistaken for meeting all of a resident's needs.
    """
    checks = _check_patterns(scenario, plan, patterns)
    failed = [check.pattern for check in checks if not check.verified]
    candidates = _candidate_changes(scenario, patterns, failed)

    def rescued_by(changes: Sequence[Change], among: Sequence[int]) -> list[int]:
        return [i for i in among if _has_verified_plan(_world(scenario, patterns[i], changes), plan)]

    return CareSweep(
        scenario=scenario.id,
        capacity_step=CAPACITY_STEP,
        patterns=tuple(checks),
        verified=len(patterns) - len(failed),
        fixes=_single_fixes(candidates, failed, rescued_by),
        steps=_greedy_steps(candidates, failed, len(patterns), rescued_by),
    )
