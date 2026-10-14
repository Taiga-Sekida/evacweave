import json
from pathlib import Path

import pytest

from packages.domain.models import Plan, Scenario
from packages.simulation.engine import simulate


def parse_s(data):
    return Scenario.model_validate_json(json.dumps(data))


def parse_p(data):
    return Plan.model_validate_json(json.dumps(data))


def rejected(result):
    return [e.reason for e in result.events if e.kind == "rejected"]


def test_blocked_route_golden(scenario, plan):
    expected = json.loads(
        (Path(__file__).resolve().parents[1] / "data/demo_area/blocked_route/expected.json").read_text()
    )
    result = simulate(scenario, plan)
    assert result.status == expected["status"]
    assert list(result.evacuated) == expected["evacuated"]
    assert list(result.not_evacuated) == expected["not_evacuated"]
    assert [e.reason for e in result.events] == expected["event_reasons"]
    assert [p.node for p in result.final_locations] == expected["final_nodes"]


def test_repaired_plan_same_conditions_passes(scenario, repaired):
    result = simulate(scenario, repaired)
    assert result.status == "PASS"
    assert result.evacuated == ("resident_01", "resident_02")
    assert not result.not_evacuated
    assert [e.time_s for e in result.events if e.kind == "arrived"] == [90, 90]


def test_deterministic_and_no_input_mutation(scenario, repaired):
    before = scenario.model_dump_json(), repaired.model_dump_json()
    results = [simulate(scenario, repaired).model_dump_json() for _ in range(10)]
    assert len(set(results)) == 1
    assert before == (scenario.model_dump_json(), repaired.model_dump_json())


def test_empty_plan_is_not_success(scenario):
    result = simulate(scenario, Plan(id="empty", intents=()))
    assert result.status == "FAIL"
    assert len(result.not_evacuated) == 2


def test_capacity_and_tie_break(scenario_data, repaired):
    scenario_data["shelters"][1]["capacity"] = 1
    result = simulate(parse_s(scenario_data), repaired)
    assert result.evacuated == ("resident_01",)
    assert rejected(result) == ["shelter_full"]
    assert all(p.node == "shelter_b_node" for p in result.final_locations)


def test_arrival_order_beats_actor_sort(scenario_data, repaired):
    scenario_data["shelters"][1]["capacity"] = 1
    data = repaired.model_dump(mode="json")
    data["intents"][0]["departure_s"] = 5
    result = simulate(parse_s(scenario_data), parse_p(data))
    assert result.evacuated == ("resident_02",)


@pytest.mark.parametrize(("opens", "passed"), [(0, True), (90, True), (91, False)])
def test_shelter_open_boundary(scenario_data, repaired, opens, passed):
    scenario_data["shelters"][1]["opens_at_s"] = opens
    result = simulate(parse_s(scenario_data), repaired)
    assert (result.status == "PASS") is passed
    if not passed:
        assert rejected(result) == ["shelter_not_open"] * 2


@pytest.mark.parametrize(("deadline", "passed"), [(89, False), (90, True)])
def test_deadline_boundary(scenario_data, repaired, deadline, passed):
    scenario_data["deadline_s"] = deadline
    result = simulate(parse_s(scenario_data), repaired)
    assert (result.status == "PASS") is passed


@pytest.mark.parametrize(
    ("start", "end", "departure", "passed"),
    [
        (0, 120, 120, True),
        (0, 120, 119, False),
        (60, 120, 0, True),
        (59, 120, 0, False),
        (20, 30, 0, False),
    ],
)
def test_half_open_closure_intersection(scenario_data, plan_data, start, end, departure, passed):
    scenario_data["roads"][0]["closures"] = [{"start_s": start, "end_s": end}]
    for intent in plan_data["intents"]:
        intent["departure_s"] = departure
    result = simulate(parse_s(scenario_data), parse_p(plan_data))
    assert (result.status == "PASS") is passed


def test_later_edge_closure_is_checked(scenario_data, repaired):
    scenario_data["roads"][2]["closures"] = [{"start_s": 60, "end_s": 61}]
    result = simulate(parse_s(scenario_data), repaired)
    assert rejected(result) == ["route_blocked"] * 2


def test_double_booking_actor(scenario, repaired):
    data = repaired.model_dump(mode="json")
    data["intents"].insert(1, data["intents"][0].copy())
    result = simulate(scenario, parse_p(data))
    assert rejected(result) == ["actor_busy"]
    assert len(result.evacuated) == 2
    assert result.status == "FAIL"  # All arriving does not erase an invalid instruction.


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        ("actor_id", "unknown", "unknown_actor"),
        ("shelter_id", "unknown", "unknown_shelter"),
        ("route", ["missing"], "unknown_road"),
        ("route", ["road_b2"], "disconnected_route"),
        ("route", ["road_b1"], "wrong_destination"),
        ("departure_s", 181, "departure_after_deadline"),
    ],
)
def test_invalid_intents_fail_without_teleport(scenario, repaired, field, value, reason):
    data = repaired.model_dump(mode="json")
    data["intents"][0][field] = value
    result = simulate(scenario, parse_p(data))
    assert reason in rejected(result)
    assert result.status == "FAIL"
    assert result.final_locations[0].node == "home"


def test_zero_capacity(scenario_data, repaired):
    scenario_data["shelters"][1]["capacity"] = 0
    assert not simulate(parse_s(scenario_data), repaired).evacuated


def test_digest_changes_with_conditions(scenario_data, repaired):
    before = simulate(parse_s(scenario_data), repaired)
    scenario_data["shelters"][1]["capacity"] = 1
    after = simulate(parse_s(scenario_data), repaired)
    assert before.input_digest != after.input_digest


def test_already_evacuated_cannot_depart_again(scenario, repaired):
    data = repaired.model_dump(mode="json")
    second = data["intents"][0].copy()
    second["departure_s"] = 90
    data["intents"].append(second)
    assert "already_evacuated" in rejected(simulate(scenario, parse_p(data)))
