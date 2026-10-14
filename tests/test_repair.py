"""Repair search with waiting, its fail-closed budget, and contingency planning."""

import json

import pytest

import packages.repair.planner as planner
from packages.domain.models import Closure, Plan, Scenario
from packages.redteam.probes import MAX_CLOSURES_PER_ROAD, single_road_faults, with_road_closed
from packages.repair.contingency import plan_contingencies
from packages.repair.planner import SearchBudgetExceeded, propose_repair
from packages.simulation.engine import simulate


def _closed(scenario: Scenario, *road_ids: str) -> Scenario:
    for road_id in road_ids:
        scenario = with_road_closed(scenario, road_id)
    return scenario


def test_repair_waits_for_a_road_to_reopen(scenario, plan):
    # road_b1 is closed all window; road_a reopens at 120 and takes 60s; deadline 180.
    faulted = _closed(scenario, "road_b1")
    repaired, result = propose_repair(faulted, plan)
    assert result.status == "PASS"
    assert {(i.departure_s, i.route, i.shelter_id) for i in repaired.intents} == {
        (120, ("road_a",), "shelter_a")
    }
    assert simulate(faulted, repaired) == result  # the Core, not the planner, decides


def test_repair_prefers_earliest_arrival_without_waiting(scenario, plan):
    repaired, _ = propose_repair(scenario, plan)
    assert all(i.departure_s == 0 and i.route == ("road_b1", "road_b2") for i in repaired.intents)


def test_repair_search_budget_fails_closed(scenario, plan, monkeypatch):
    monkeypatch.setattr(planner, "MAX_SEARCH_STATES", 1)
    with pytest.raises(SearchBudgetExceeded):
        propose_repair(scenario, plan)


def test_repair_rejects_a_plan_that_already_passes(scenario, repaired):
    with pytest.raises(ValueError, match="failing plan"):
        propose_repair(scenario, repaired)


def test_departure_candidates_are_bounded(scenario_data):
    scenario_data["roads"][1]["closures"] = [{"start_s": t, "end_s": t + 1} for t in range(0, 40, 2)]
    scenario = Scenario.model_validate_json(json.dumps(scenario_data))
    candidates = planner._departure_candidates(scenario)
    assert candidates[0] == 0
    assert len(candidates) == planner.MAX_DEPARTURE_CANDIDATES
    assert list(candidates) == sorted(candidates)


def test_road_closure_limit_is_respected(scenario_data):
    scenario_data["roads"][1]["closures"] = [
        {"start_s": t, "end_s": t + 1} for t in range(MAX_CLOSURES_PER_ROAD)
    ]
    scenario = Scenario.model_validate_json(json.dumps(scenario_data))
    assert with_road_closed(scenario, "road_b1") is None
    assert with_road_closed(scenario, "road_b2") is not None


def test_contingencies_cover_each_single_point_of_failure(scenario, plan):
    repaired, _ = propose_repair(scenario, plan)
    probes = single_road_faults(scenario, repaired)
    assert [(p.road_id, p.status) for p in probes] == [("road_b1", "FAIL"), ("road_b2", "FAIL")]

    contingencies = plan_contingencies(scenario, repaired, probes)
    assert [(c.road_id, c.covered) for c in contingencies] == [("road_b1", True), ("road_b2", True)]
    for contingency in contingencies:
        faulted = _closed(scenario, contingency.road_id)
        result = simulate(faulted, contingency.plan)
        assert result.status == "PASS"
        assert result.input_digest == contingency.input_digest
        assert contingency.last_arrival_s == 180
    # Ordinal ids: a road id may already use the whole 64-character identifier.
    assert [c.plan.id for c in contingencies] == ["contingency_1", "contingency_2"]


def test_contingency_reports_when_no_fallback_exists(scenario, plan):
    # With road_a closed all window too, losing road_b1 strands everyone.
    hardened = _closed(scenario, "road_a")
    repaired, _ = propose_repair(hardened, Plan(id="empty", intents=()))
    probes = single_road_faults(hardened, repaired)
    contingencies = plan_contingencies(hardened, repaired, probes)
    assert {c.reason for c in contingencies} == {"no_verified_plan"}
    assert all(c.plan is None and not c.covered for c in contingencies)


def test_contingency_reports_search_budget_exhaustion(scenario, plan, monkeypatch):
    repaired, _ = propose_repair(scenario, plan)
    probes = single_road_faults(scenario, repaired)
    # Every queue push is charged; a fallback here needs only one push after waiting.
    monkeypatch.setattr(planner, "MAX_SEARCH_STATES", 0)
    contingencies = plan_contingencies(scenario, repaired, probes)
    assert {c.reason for c in contingencies} == {"search_budget_exhausted"}


def test_passing_probes_need_no_contingency(scenario, repaired):
    probes = single_road_faults(scenario, repaired)
    passing = tuple(p.model_copy(update={"status": "PASS"}) for p in probes)
    assert plan_contingencies(scenario, repaired, passing) == ()


def test_closure_helper_keeps_contract_checks(scenario):
    faulted = with_road_closed(scenario, "road_b2")
    road = next(r for r in faulted.roads if r.id == "road_b2")
    assert road.closures == (Closure(start_s=0, end_s=scenario.deadline_s),)
    assert faulted.roads[0] == scenario.roads[0]


def _world(nodes, roads, shelters, deadline, personas=(("p", "H"),)):
    return Scenario.model_validate_json(
        json.dumps(
            {
                "id": "w",
                "dataset_version": "v",
                "deadline_s": deadline,
                "nodes": list(nodes),
                "roads": [
                    {
                        "id": r[0],
                        "source": r[1],
                        "target": r[2],
                        "travel_time_s": r[3],
                        "closures": [{"start_s": s, "end_s": e} for s, e in (r[4] if len(r) > 4 else ())],
                    }
                    for r in roads
                ],
                "shelters": [
                    {"id": s[0], "node": s[1], "capacity": s[2], "opens_at_s": s[3]} for s in shelters
                ],
                "personas": [{"id": p, "start_node": n} for p, n in personas],
            }
        )
    )


EMPTY = Plan(id="empty", intents=())


def test_search_budget_bounds_graphs_with_many_parallel_roads():
    # 255 parallel roads each way and 254 spare nodes (raising the hop cap) used to
    # make one repair run for over a minute: pushes were not charged, only pops.
    import time

    nodes = ["H", "X"] + [f"n{i}" for i in range(254)]
    roads = [(f"f{i}", "H", "X", i) for i in range(1, 256)] + [(f"b{i}", "X", "H", i) for i in range(1, 256)]
    world = _world(nodes, roads, [("s", "n0", 1, 0)], deadline=86_400)
    started = time.monotonic()
    with pytest.raises(SearchBudgetExceeded):
        propose_repair(world, EMPTY)
    assert time.monotonic() - started < 5


def test_equal_time_states_keep_the_route_with_fewer_roads():
    # a,b,a and c both reach X at t=3; the 3-road path used to claim the state first
    # and then hit the hop cap, hiding c -> d.
    world = _world(
        ["H", "X", "Z"],
        [("a", "H", "X", 1), ("b", "X", "H", 1), ("c", "H", "X", 3), ("d", "X", "Z", 1, [(0, 3)])],
        [("s", "Z", 1, 0)],
        deadline=4,
    )
    plan, result = propose_repair(world, EMPTY)
    assert plan.intents[0].route == ("c", "d") and result.status == "PASS"


def test_repair_leaves_later_to_arrive_when_the_shelter_opens():
    world = _world(["H", "Z"], [("r", "H", "Z", 50)], [("s", "Z", 1, 100)], deadline=120)
    plan, result = propose_repair(world, EMPTY)
    assert plan.intents[0].departure_s == 50 and result.status == "PASS"


def test_long_road_ids_do_not_break_contingency_planning():
    long_id = "r" + "x" * 63  # the full 64-character identifier
    world = _world(["H", "Z"], [(long_id, "H", "Z", 10), ("alt", "H", "Z", 20)], [("s", "Z", 1, 0)], 200)
    plan = Plan.model_validate_json(
        json.dumps({"id": "p", "intents": [{"actor_id": "p", "shelter_id": "s", "route": [long_id]}]})
    )
    (contingency,) = plan_contingencies(world, plan, single_road_faults(world, plan))
    assert contingency.covered and contingency.plan.intents[0].route == ("alt",)


def test_residents_with_fewer_options_are_not_stranded_by_greedy_choices():
    # p1 reaches both shelters, p2 only s1; s1 has one bed. Assigning in id order with
    # earliest arrival first gave s1 to p1 and stranded p2.
    world = _world(
        ["A", "B", "S1", "S2"],
        [("a1", "A", "S1", 10), ("a2", "A", "S2", 20), ("b1", "B", "S1", 10)],
        [("s1", "S1", 1, 0), ("s2", "S2", 1, 0)],
        deadline=100,
        personas=(("p1", "A"), ("p2", "B")),
    )
    plan, result = propose_repair(world, EMPTY)
    assert {i.actor_id: i.shelter_id for i in plan.intents} == {"p1": "s2", "p2": "s1"}
    assert result.status == "PASS"


def test_augmenting_paths_move_earlier_occupants_to_make_room():
    # Beds: A, B, C one each. p1 prefers A then B, p2 prefers B then C, p3 prefers A then B.
    # Every resident has two options, so ordering alone cannot help: first-available
    # gives p1 A and p2 B and strands p3. Augmenting moves p2 to C and p1 to B.
    world = _world(
        ["X", "Y", "Z", "A", "B", "C"],
        [
            ("xa", "X", "A", 10),
            ("xb", "X", "B", 20),
            ("yb", "Y", "B", 10),
            ("yc", "Y", "C", 20),
            ("za", "Z", "A", 10),
            ("zb", "Z", "B", 20),
        ],
        [("a", "A", 1, 0), ("b", "B", 1, 0), ("c", "C", 1, 0)],
        deadline=100,
        personas=(("p1", "X"), ("p2", "Y"), ("p3", "Z")),
    )
    plan, result = propose_repair(world, EMPTY)
    assert {i.actor_id: i.shelter_id for i in plan.intents} == {"p1": "b", "p2": "c", "p3": "a"}
    assert result.status == "PASS"


def test_infeasible_capacity_is_reported_not_overbooked():
    world = _world(
        ["A", "S1"], [("a1", "A", "S1", 10)], [("s1", "S1", 1, 0)], 100, personas=(("p1", "A"), ("p2", "A"))
    )
    assert propose_repair(world, EMPTY) is None


def test_a_free_second_choice_is_used_before_moving_anyone():
    # A: S1 at 10 s or S2 at 1000 s. B: S1 at 10 s or S3 at 20 s. One bed each.
    # B must take its free S3 rather than push A to the 1000 s shelter.
    world = _world(
        ["HA", "HB", "S1", "S2", "S3"],
        [("a1", "HA", "S1", 10), ("a2", "HA", "S2", 1000), ("b1", "HB", "S1", 10), ("b3", "HB", "S3", 20)],
        [("s1", "S1", 1, 0), ("s2", "S2", 1, 0), ("s3", "S3", 1, 0)],
        deadline=2000,
        personas=(("A", "HA"), ("B", "HB")),
    )
    plan, _ = propose_repair(world, EMPTY)
    assert {i.actor_id: i.shelter_id for i in plan.intents} == {"A": "s1", "B": "s3"}


def test_a_slower_road_can_be_shifted_to_the_opening_time():
    # The shelter opens at 100 s. Road f (10 s) is closed 95-115 s, so shifting it to
    # arrive at 100 s fails; leaving at 50 s on road g (50 s) works.
    world = _world(
        ["H", "Z"],
        [("f", "H", "Z", 10, [(95, 115)]), ("g", "H", "Z", 50)],
        [("s", "Z", 1, 100)],
        deadline=120,
    )
    plan, result = propose_repair(world, EMPTY)
    assert (plan.intents[0].route, plan.intents[0].departure_s) == (("g",), 50)
    assert result.status == "PASS"
