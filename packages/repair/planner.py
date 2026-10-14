"""Bounded deterministic repair candidate search for synthetic evacuation plans.

This is a CPU heuristic, NOT a trained Repair AI and not an optimality guarantee.
Every proposed plan is independently checked by the Simulation Core.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from heapq import heappop, heappush
from typing import NamedTuple

from packages.domain.models import MoveIntent, Persona, Plan, Road, Scenario, SimulationResult
from packages.simulation.engine import simulate

# Queue pushes allowed for one propose_repair() call, across every resident and
# departure time. Every push is charged, so the work is bounded even on graphs with
# many parallel roads. Exhausting it raises instead of returning a partial answer.
MAX_SEARCH_STATES = 50_000
# Departure times tried per resident: 0, road reopenings and shelter openings.
MAX_DEPARTURE_CANDIDATES = 8

# Arrivals kept per shelter node and departure while none is late enough for the shelter
# to be open: an early route may be blocked when shifted later, while a slower one is not.
MAX_ARRIVALS_PER_TARGET = 4

Route = tuple[str, ...]
Arrivals = dict[str, list[tuple[int, Route]]]


class SearchBudgetExceeded(RuntimeError):
    """The bounded search gave up; this is not proof that no plan exists."""


class _StateBudget:
    def __init__(self, limit: int) -> None:
        self.remaining = limit

    def spend(self) -> None:
        if self.remaining <= 0:
            raise SearchBudgetExceeded("repair search state budget exhausted")
        self.remaining -= 1


def _departure_candidates(scenario: Scenario) -> tuple[int, ...]:
    # Waiting only helps when something changes: a closure ends or a shelter opens.
    times = {0}
    times.update(closure.end_s for road in scenario.roads for closure in road.closures)
    times.update(shelter.opens_at_s for shelter in scenario.shelters)
    return tuple(sorted(t for t in times if t <= scenario.deadline_s))[:MAX_DEPARTURE_CANDIDATES]


def _blocked(road: Road, start: int, end: int) -> bool:
    # Same half-open [start, end) rule as the Simulation Core.
    return any(start < c.end_s and c.start_s < end for c in road.closures)


def _earliest_arrivals_by_node(
    scenario: Scenario,
    start: str,
    departure: int,
    roads_by_source: dict[str, list[Road]],
    ready_at: dict[str, int],
    budget: _StateBudget,
) -> Arrivals:
    """Feasible arrivals at each shelter node for one departure time, earliest first.

    The engine has no waiting at intermediate nodes, so the search is over
    time-labelled states: an earlier arrival can intersect a closure that a later
    one avoids. States pop in (arrival, hops, insertion) order, so arrivals at a target
    come out earliest first, and a state reached first is reached with the fewest
    roads, which leaves it the most room under the route-length cap. A target is done
    once an arrival reaches its opening time (ready_at) or MAX_ARRIVALS_PER_TARGET
    arrivals are kept. Routes are kept as parent links, not copied tuples.
    """
    trail: list[tuple[int, str]] = []  # (parent index, road id); index -1 is the start
    queue: list[tuple[int, int, int, str]] = [(departure, 0, -1, start)]
    visited: set[tuple[str, int]] = set()
    found: Arrivals = {node: [] for node in ready_at}
    done: set[str] = set()

    def route_of(index: int) -> Route:
        roads: list[str] = []
        while index >= 0:
            index, road_id = trail[index]
            roads.append(road_id)
        return tuple(reversed(roads))

    while queue and len(done) < len(ready_at):
        arrival, hops, index, node = heappop(queue)
        if (node, arrival) in visited:
            continue
        visited.add((node, arrival))

        if node in ready_at and hops and node not in done:
            found[node].append((arrival, route_of(index)))
            if arrival >= ready_at[node] or len(found[node]) >= MAX_ARRIVALS_PER_TARGET:
                done.add(node)
        if hops >= len(scenario.nodes):
            continue

        for road in roads_by_source.get(node, []):
            end = arrival + road.travel_time_s
            if end > scenario.deadline_s or _blocked(road, arrival, end):
                continue
            budget.spend()
            trail.append((index, road.id))
            heappush(queue, (end, hops + 1, len(trail) - 1, road.target))
    return found


def _fixed_route_arrival(route: Route, departure: int, roads: dict[str, Road], deadline: int) -> int | None:
    """Arrival time of a fixed route for a given departure, or None if it is blocked."""
    clock = departure
    for road_id in route:
        road = roads[road_id]
        end = clock + road.travel_time_s
        if end > deadline or _blocked(road, clock, end):
            return None
        clock = end
    return clock


class Candidate(NamedTuple):
    """One way for a resident to reach one shelter.

    Compared as a plain tuple, field by field: the earliest arrival wins, then the fewest
    roads, then the earliest departure.
    """

    arrival: int
    roads: int
    departure: int
    shelter_id: str
    route: Route


def _assign(candidates: dict[str, list[Candidate]], capacity: dict[str, int]) -> dict[str, Candidate] | None:
    """Capacity-aware assignment by augmenting paths (Kuhn's algorithm with b-matching).

    Each resident takes their earliest-arrival shelter that still has room. Only when
    every shelter they can reach is full is an occupant moved to another of *their*
    options to make room, so a resident with few options is never stranded by one with
    many. This maximises how many residents are placed; it does not minimise arrival
    times (no optimality is claimed).
    Returns None only when no assignment of the computed routes fits the capacities.
    Residents with the fewest options go first; ties break on actor id.
    """
    occupants: dict[str, list[str]] = {shelter: [] for shelter in capacity}
    chosen: dict[str, Candidate] = {}

    def place(actor: str, tried: set[str]) -> bool:
        options = [candidate for candidate in candidates[actor] if candidate.shelter_id not in tried]
        # A free bed, in the resident's own preference order, needs nobody to move.
        for candidate in options:
            if len(occupants[candidate.shelter_id]) < capacity[candidate.shelter_id]:
                occupants[candidate.shelter_id].append(actor)
                chosen[actor] = candidate
                return True
        for candidate in options:
            shelter = candidate.shelter_id
            if shelter in tried:
                continue
            tried.add(shelter)
            for other in sorted(occupants[shelter]):
                if place(other, tried):
                    occupants[shelter].remove(other)
                    occupants[shelter].append(actor)
                    chosen[actor] = candidate
                    return True
        return False

    for actor in sorted(candidates, key=lambda a: (len(candidates[a]), a)):
        if not place(actor, set()):
            return None
    return chosen


def _roads_by_source(scenario: Scenario) -> dict[str, list[Road]]:
    """Each node's outgoing roads, sorted by id so the search expands them in a fixed order."""
    roads_by_source: dict[str, list[Road]] = defaultdict(list)
    for road in scenario.roads:
        roads_by_source[road.source].append(road)
    for roads in roads_by_source.values():
        roads.sort(key=lambda r: r.id)
    return roads_by_source


def _ready_at(scenario: Scenario) -> dict[str, int]:
    """Each shelter node and the time from which every shelter there is open."""
    ready_at: dict[str, int] = {}
    for shelter in scenario.shelters:
        ready_at[shelter.node] = max(ready_at.get(shelter.node, 0), shelter.opens_at_s)
    return ready_at


def _best_option_per_shelter(
    scenario: Scenario,
    persona: Persona,
    departures: tuple[int, ...],
    arrivals_from: Callable[[str, int], Arrivals],
    roads_by_id: dict[str, Road],
    restricted: set[tuple[str, str]],
) -> dict[str, Candidate]:
    """One resident's smallest Candidate for each shelter they may be sent to, over every departure."""
    best: dict[str, Candidate] = {}
    for departure in departures:
        arrivals = arrivals_from(persona.start_node, departure)
        for shelter in scenario.shelters:
            if shelter.capacity == 0 or (persona.id, shelter.id) in restricted:
                continue
            for arrival, road_ids in arrivals[shelter.node]:
                start = departure
                if arrival < shelter.opens_at_s:
                    # Leave later so the same route arrives exactly when the shelter opens.
                    start = departure + shelter.opens_at_s - arrival
                    if _fixed_route_arrival(road_ids, start, roads_by_id, scenario.deadline_s) is None:
                        continue
                    arrival = shelter.opens_at_s
                option = Candidate(arrival, len(road_ids), start, shelter.id, road_ids)
                if shelter.id not in best or option < best[shelter.id]:
                    best[shelter.id] = option
    return best


def propose_repair(
    scenario: Scenario, rejected_plan: Plan, *, plan_id: str = "auto_repair"
) -> tuple[Plan, SimulationResult] | None:
    """Return a verified candidate, or None when this bounded search finds none.

    For every resident the search finds the earliest arrival at each shelter: they may
    wait at home until a road reopens, or leave later so that a route arrives exactly
    when its shelter opens; for equal arrival, fewer roads win, so waiting at home beats
    driving loops. Residents are then assigned to shelters within capacity (see
    _assign). rejected_plan is only checked to FAIL: every resident is planned from
    scratch, and the original plan is not modified. Raises SearchBudgetExceeded when the
    search gives up. A PASS has no real-world authority.
    """
    scenario = Scenario.model_validate(scenario)
    rejected_plan = Plan.model_validate(rejected_plan)
    if simulate(scenario, rejected_plan).status != "FAIL":
        raise ValueError("repair requires a failing plan")

    roads_by_id = {road.id: road for road in scenario.roads}
    roads_by_source = _roads_by_source(scenario)
    ready_at = _ready_at(scenario)
    departures = _departure_candidates(scenario)
    restricted = {(r.actor_id, r.shelter_id) for r in scenario.restrictions}
    # One budget for every resident and departure of this call.
    budget = _StateBudget(MAX_SEARCH_STATES)
    searched: dict[tuple[str, int], Arrivals] = {}

    def arrivals_from(start: str, departure: int) -> Arrivals:
        # Residents who share a home share one search per departure time.
        key = (start, departure)
        if key not in searched:
            searched[key] = _earliest_arrivals_by_node(
                scenario, start, departure, roads_by_source, ready_at, budget
            )
        return searched[key]

    candidates: dict[str, list[Candidate]] = {}
    for persona in scenario.personas:
        best = _best_option_per_shelter(scenario, persona, departures, arrivals_from, roads_by_id, restricted)
        if not best:
            return None
        candidates[persona.id] = sorted(best.values())

    chosen = _assign(candidates, {shelter.id: shelter.capacity for shelter in scenario.shelters})
    if chosen is None:
        return None
    intents = tuple(
        MoveIntent(
            actor_id=actor,
            shelter_id=candidate.shelter_id,
            departure_s=candidate.departure,
            route=candidate.route,
        )
        for actor, candidate in sorted(chosen.items())
    )
    plan = Plan(id=plan_id, intents=intents)
    result = simulate(scenario, plan)
    # Never return an unverified repair as successful.
    return (plan, result) if result.status == "PASS" else None
