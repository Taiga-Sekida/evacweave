"""Pure, deterministic fixed-network model. No real-world safety authority."""

from __future__ import annotations

import hashlib
import heapq
import json

from packages.domain.models import (
    Event,
    MoveIntent,
    Plan,
    Position,
    Scenario,
    SimulationResult,
    Verdict,
)

_ARRIVAL = 0  # handled before a departure at the same second
_DEPARTURE = 1


def _input_digest(scenario: Scenario, plan: Plan) -> str:
    # Canonical model input, no wall-clock time, random ID, or LLM output generation.
    canonical = json.dumps(
        {"scenario": scenario.model_dump(mode="json"), "plan": plan.model_dump(mode="json")},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def simulate(scenario: Scenario, plan: Plan) -> SimulationResult:
    """Judge one plan in one scenario.

    Intents are replayed in time order: each departure is checked (the actor, the
    deadline, the shelter and the resident's restrictions, the route and its closures),
    each arrival against the shelter's opening time and capacity. PASS only when every
    persona is admitted and nothing was rejected. No wall clock, randomness or model
    output, so equal inputs give an equal result and input_digest.
    """
    scenario = Scenario.model_validate(scenario)
    plan = Plan.model_validate(plan)
    roads = {road.id: road for road in scenario.roads}
    shelters = {shelter.id: shelter for shelter in scenario.shelters}
    positions = {persona.id: persona.start_node for persona in scenario.personas}
    occupied = {shelter.id: 0 for shelter in scenario.shelters}
    restricted = {(r.actor_id, r.shelter_id) for r in scenario.restrictions}
    moving: set[str] = set()
    evacuated: set[str] = set()
    events: list[Event] = []
    # (time, _ARRIVAL or _DEPARTURE, actor, input index, intent): stable actor + input index tie-break.
    # A departure after the deadline is queued at the deadline, where it is rejected.
    queue: list[tuple[int, int, str, int, MoveIntent]] = []
    for index, intent in enumerate(plan.intents):
        heapq.heappush(
            queue, (min(intent.departure_s, scenario.deadline_s), _DEPARTURE, intent.actor_id, index, intent)
        )

    def reject(time_s: int, intent: MoveIntent, reason: str) -> None:
        events.append(
            Event(
                time_s=time_s,
                actor_id=intent.actor_id,
                kind="rejected",
                reason=reason,
                node=positions.get(intent.actor_id),
            )
        )

    def check_route(intent: MoveIntent, departure: int) -> tuple[int, str | None]:
        node = positions[intent.actor_id]
        clock = departure
        for road_id in intent.route:
            road = roads.get(road_id)
            if road is None:
                return clock, "unknown_road"
            if road.source != node:
                return clock, "disconnected_route"
            end = clock + road.travel_time_s
            # Known, fixed closure schedule. Half-open intervals [start, end).
            if any(clock < closure.end_s and closure.start_s < end for closure in road.closures):
                return clock, "route_blocked"
            node, clock = road.target, end
        if node != shelters[intent.shelter_id].node:
            return clock, "wrong_destination"
        if clock > scenario.deadline_s:
            return clock, "deadline_exceeded"
        return clock, None

    def arrive(time_s: int, actor_id: str, intent: MoveIntent) -> None:
        shelter = shelters[intent.shelter_id]
        positions[actor_id] = shelter.node
        moving.remove(actor_id)
        if time_s < shelter.opens_at_s:
            reject(time_s, intent, "shelter_not_open")
        elif occupied[shelter.id] >= shelter.capacity:
            reject(time_s, intent, "shelter_full")
        else:
            occupied[shelter.id] += 1
            evacuated.add(actor_id)
            events.append(
                Event(time_s=time_s, actor_id=actor_id, kind="arrived", reason="admitted", node=shelter.node)
            )

    def depart(time_s: int, actor_id: str, index: int, intent: MoveIntent) -> None:
        if actor_id not in positions:
            reject(time_s, intent, "unknown_actor")
        elif intent.departure_s > scenario.deadline_s:
            reject(time_s, intent, "departure_after_deadline")
        elif actor_id in evacuated:
            reject(time_s, intent, "already_evacuated")
        elif actor_id in moving:
            reject(time_s, intent, "actor_busy")
        elif intent.shelter_id not in shelters:
            reject(time_s, intent, "unknown_shelter")
        elif (actor_id, intent.shelter_id) in restricted:
            reject(time_s, intent, "unsuitable_shelter")
        else:
            arrival, error = check_route(intent, time_s)
            if error:
                reject(time_s, intent, error)
                return
            moving.add(actor_id)
            events.append(
                Event(
                    time_s=time_s,
                    actor_id=actor_id,
                    kind="departed",
                    reason="route_validated",
                    node=positions[actor_id],
                )
            )
            heapq.heappush(queue, (arrival, _ARRIVAL, actor_id, index, intent))

    while queue:
        time_s, kind, actor_id, index, intent = heapq.heappop(queue)
        if kind == _ARRIVAL:
            arrive(time_s, actor_id, intent)
        else:
            depart(time_s, actor_id, index, intent)

    missing = set(positions) - evacuated
    status: Verdict = "FAIL" if missing or any(e.kind == "rejected" for e in events) else "PASS"
    return SimulationResult(
        scenario_id=scenario.id,
        plan_id=plan.id,
        input_digest=_input_digest(scenario, plan),
        status=status,
        evacuated=tuple(sorted(evacuated)),
        not_evacuated=tuple(sorted(missing)),
        final_locations=tuple(Position(actor_id=actor, node=positions[actor]) for actor in sorted(positions)),
        events=tuple(events),
    )
