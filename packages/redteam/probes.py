"""Deterministic single-road-closure probes; NOT a trained Red Team AI."""

from __future__ import annotations

import json

from packages.domain.agent import MAX_PROBES, FaultProbe
from packages.domain.models import MAX_CLOSURES_PER_ROAD, Closure, Plan, Scenario
from packages.simulation.engine import simulate


def with_road_closed(scenario: Scenario, road_id: str) -> Scenario | None:
    """Return the scenario with one road closed for the whole evacuation window.

    Returns None when the road cannot take another closure. The result is re-parsed
    from JSON so the modified world passes the same contract checks as any input.
    """
    roads = []
    for road in scenario.roads:
        if road.id == road_id:
            if len(road.closures) >= MAX_CLOSURES_PER_ROAD:
                return None
            road = road.model_copy(
                update={"closures": (*road.closures, Closure(start_s=0, end_s=scenario.deadline_s))}
            )
        roads.append(road.model_dump(mode="json"))
    return Scenario.model_validate_json(
        json.dumps({**scenario.model_dump(mode="json"), "roads": roads}, ensure_ascii=False)
    )


def with_roads_closed(scenario: Scenario, road_ids: tuple[str, ...]) -> Scenario | None:
    """Close several of the scenario's own roads for the whole window, in order.

    None when a road is unknown, repeated or cannot take another closure.
    """
    if len(set(road_ids)) != len(road_ids) or not set(road_ids) <= {road.id for road in scenario.roads}:
        return None
    world = scenario
    for road_id in road_ids:
        closed = with_road_closed(world, road_id)
        if closed is None:
            return None
        world = closed
    return world


def single_road_faults(scenario: Scenario, passing_plan: Plan, *, limit: int = 8) -> tuple[FaultProbe, ...]:
    """Re-run a passing plan after one additional full-window road closure.

    Probes are synthetic and non-destructive. Every result comes from simulate().
    Results are ordered by road ID so repeated runs are byte-for-byte stable.
    The default limit serves tests; the agent loop probes up to MAX_PROBES roads.
    """
    scenario = Scenario.model_validate(scenario)
    passing_plan = Plan.model_validate(passing_plan)
    if not 1 <= limit <= MAX_PROBES:
        raise ValueError(f"probe limit must be between 1 and {MAX_PROBES}")
    if simulate(scenario, passing_plan).status != "PASS":
        raise ValueError("fault probes require a passing plan")

    used = sorted({road for intent in passing_plan.intents for road in intent.route})
    probes: list[FaultProbe] = []
    for road_id in used[:limit]:
        faulted = with_road_closed(scenario, road_id)
        if faulted is None:
            continue
        result = simulate(faulted, passing_plan)
        probes.append(
            FaultProbe(
                road_id=road_id,
                status=result.status,
                not_evacuated=result.not_evacuated,
                rejection_reasons=tuple(sorted({e.reason for e in result.events if e.kind == "rejected"})),
                input_digest=result.input_digest,
            )
        )
    return tuple(probes)
