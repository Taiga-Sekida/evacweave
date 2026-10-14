"""Road-closure challenge: visitors close roads, the rule-policy agent answers via the Core."""

import pytest
from fastapi.testclient import TestClient

import apps.api.main as api
from apps.api.main import app
from packages.domain.live import ChallengeRun

client = TestClient(app)


def _challenge(scenario="base", *roads):
    return client.post("/v1/agent/challenge", json={"scenario": scenario, "closed_roads": list(roads)})


def test_the_agent_waits_for_a_reopening_when_the_detour_is_closed():
    response = _challenge("base", "road_b1")
    assert response.status_code == 200, response.text
    run = ChallengeRun.model_validate_json(response.text)
    closed = next(road for road in run.world.roads if road.id == "road_b1")
    assert (closed.closures[-1].start_s, closed.closures[-1].end_s) == (0, run.world.deadline_s)
    assert run.trace.policy == "rule-policy"
    assert run.trace.final_result is not None and run.trace.final_result.status == "PASS"
    # The only way left is road_a, closed until it reopens: the repair waits at home.
    assert {(i.route, i.departure_s) for i in run.trace.final_plan.intents} == {(("road_a",), 120)}


def test_closing_every_way_out_hands_over_without_a_plan():
    run = ChallengeRun.model_validate_json(_challenge("base", "road_a", "road_b1").text)
    assert run.trace.outcome == "escalated"
    assert run.trace.final_plan is None and run.trace.final_result is None
    assert run.trace.steps[-1].decision.reason_code == "no_verified_plan"


def test_the_district_challenge_never_finishes_a_failing_plan():
    run = ChallengeRun.model_validate_json(_challenge("river_district", "north_exit").text)
    assert run.trace.scenario_id == run.world.id
    for step in run.trace.steps:
        if step.decision and step.decision.action == "finish":
            assert step.observation.plan_status == "PASS"


def test_road_order_does_not_matter_and_answers_are_cached():
    api._challenge.cache_clear()
    first = _challenge("base", "road_b1", "road_b2")
    second = _challenge("base", "road_b2", "road_b1")
    assert first.json() == second.json()
    assert first.json()["closed_roads"] == ["road_b1", "road_b2"]
    assert api._challenge.cache_info().hits == 1


@pytest.mark.parametrize(
    "payload",
    [
        {"scenario": "base", "closed_roads": []},
        {"scenario": "base", "closed_roads": ["road_a", "road_b1", "road_b2"]},
        {"scenario": "base", "closed_roads": ["road_a", "road_a"]},
        {"scenario": "base", "closed_roads": ["north_exit"]},
        {"scenario": "elsewhere", "closed_roads": ["road_a"]},
        {"scenario": "base", "closed_roads": ["road_a"], "plan": {}},
    ],
)
def test_only_roads_of_a_reviewed_scenario_can_be_closed(payload):
    assert client.post("/v1/agent/challenge", json=payload).status_code == 422
