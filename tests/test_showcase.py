import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import apps.api.main as api
from apps.api.main import MAX_PUBLIC_BODY, app
from packages.agents.loop import run_agent
from packages.domain.agent import AgentDecision, AgentTrace, RecordedAgentRun
from packages.domain.models import Closure, Plan, Scenario
from packages.redteam.probes import single_road_faults
from packages.repair.planner import propose_repair
from packages.simulation.engine import simulate

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "demo_area" / "blocked_route"
client = TestClient(app)


def load():
    scenario = Scenario.model_validate_json((DATA / "scenario.json").read_text())
    baseline = Plan.model_validate_json((DATA / "plan.json").read_text())
    return scenario, baseline


def test_repair_is_real_reverified_core_result():
    scenario, baseline = load()
    snapshot = scenario.model_dump_json()
    outcome = propose_repair(scenario, baseline)
    assert outcome is not None
    plan, result = outcome
    assert result.status == "PASS"
    assert plan.id == "auto_repair"
    assert all(i.route == ("road_b1", "road_b2") for i in plan.intents)
    assert result.input_digest == simulate(scenario, plan).input_digest
    assert scenario.model_dump_json() == snapshot


def test_no_route_fails_closed():
    # Every road is closed for the whole window. (Closing only road_b1/road_b2 is no
    # longer infeasible: the planner may wait for road_a to reopen; see test_repair.py.)
    scenario, baseline = load()
    roads = [
        road.model_copy(update={"closures": (*road.closures, Closure(start_s=0, end_s=180))})
        for road in scenario.roads
    ]
    blocked = Scenario.model_validate_json(
        json.dumps(
            {
                **scenario.model_dump(mode="json"),
                "roads": [road.model_dump(mode="json") for road in roads],
            }
        )
    )
    assert propose_repair(blocked, baseline) is None


def test_redteam_probe_single_faults_are_deterministic():
    scenario, baseline = load()
    plan, result = propose_repair(scenario, baseline)
    assert result.status == "PASS"
    first = single_road_faults(scenario, plan)
    assert {p.road_id for p in first} == {"road_b1", "road_b2"}
    assert all(p.status == "FAIL" for p in first)
    assert all(p.not_evacuated == ("resident_01", "resident_02") for p in first)
    assert first == single_road_faults(scenario, plan)
    with pytest.raises(ValueError):
        single_road_faults(scenario, baseline)


def test_showcase_grounded_and_explicitly_not_ai():
    response = client.get("/v1/showcase")
    assert response.status_code == 200
    doc = response.json()
    assert doc["schema_version"] == "evacweave-showcase/v2"
    assert doc["evidence_kind"] == "synthetic-deterministic"
    assert doc["live_ai_used"] is False
    assert doc["baseline_result"]["status"] == "FAIL"
    assert doc["repaired_result"]["status"] == "PASS"
    assert len(doc["probes"]) == 2
    assert [e["time_s"] for e in doc["repaired_result"]["events"] if e["kind"] == "arrived"] == [90, 90]
    scenario = Scenario.model_validate_json(json.dumps(doc["scenario"]))
    repair = Plan.model_validate_json(json.dumps(doc["repaired_plan"]))
    assert simulate(scenario, repair).input_digest == doc["repaired_result"]["input_digest"]


def test_showcase_is_one_rule_policy_agent_run():
    doc = client.get("/v1/showcase").json()
    trace = AgentTrace.model_validate_json(json.dumps(doc["agent_trace"]))
    assert trace.policy == "rule-policy" and trace.model is None and trace.model_calls == 0
    assert [s.decision.action for s in trace.steps] == [
        "run_repair",
        "run_redteam",
        "plan_contingency",
        "finish",
    ]
    assert trace.outcome == "verified_with_contingencies"
    assert doc["repaired_plan"] == json.loads(trace.final_plan.model_dump_json())
    assert [c["road_id"] for c in doc["contingencies"]] == ["road_b1", "road_b2"]
    assert all(c["reason"] == "verified_alternative" for c in doc["contingencies"])


def _recorded(scenario, plan, *decisions) -> RecordedAgentRun:
    class Scripted:
        name, model, prompt_version = "vertex-gemini", "gemini-3.5-flash", "orchestrator/v2"

        def __init__(self):
            self.model_calls = 0

        async def decide(self, observation):
            self.model_calls += 1
            action, reason = decisions[self.model_calls - 1]
            return AgentDecision(action=action, reason_code=reason)

    trace = asyncio.run(run_agent(scenario, plan, Scripted()))
    return RecordedAgentRun(recorded_at="2026-10-13T01:02:03Z", service_revision="abc1234", trace=trace)


GEMINI_DECISIONS = (
    ("run_repair", "plan_failed_verification"),
    ("run_redteam", "check_robustness"),
    ("plan_contingency", "cover_single_points_of_failure"),
    ("finish", "verified_plan_ready"),
)


@pytest.fixture
def recorded_path(tmp_path, monkeypatch):
    path = tmp_path / "recorded_gemini_run.json"
    monkeypatch.setattr(api, "RECORDED_RUN_PATH", path)
    api._showcase.cache_clear()
    yield path
    api._showcase.cache_clear()


def test_showcase_without_a_recording_says_so(recorded_path):
    doc = client.get("/v1/showcase").json()
    assert doc["recorded_ai_run"] is None and doc["recorded_ai_run_status"] == "absent"


def test_committed_gemini_recording_passes_the_public_replay():
    # data/evidence/recorded_gemini_run.json: a real run on Vertex AI, recorded by
    # scripts/record_gemini_run.py. The public service shows it only if replay reproduces it.
    assert api.RECORDED_RUN_PATH == ROOT / "data" / "evidence" / "recorded_gemini_run.json"
    api._showcase.cache_clear()
    try:
        doc = client.get("/v1/showcase").json()
    finally:
        api._showcase.cache_clear()
    assert doc["recorded_ai_run_status"] == "replay_verified"
    trace = doc["recorded_ai_run"]["trace"]
    assert trace["policy"] == "vertex-gemini" and trace["model"].startswith("gemini-")
    assert 1 <= trace["model_calls"] <= 6
    assert doc["live_ai_used"] is False  # replayed, not called


def test_recorded_gemini_run_is_shown_after_replay(recorded_path):
    scenario, baseline = load()
    recorded = _recorded(scenario, baseline, *GEMINI_DECISIONS)
    recorded_path.write_text(recorded.model_dump_json(), encoding="utf-8")
    doc = client.get("/v1/showcase").json()
    assert doc["recorded_ai_run_status"] == "replay_verified"
    assert doc["recorded_ai_run"]["trace"]["policy"] == "vertex-gemini"
    assert doc["live_ai_used"] is False  # replayed, not called


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda d: d["trace"]["steps"][0]["effect"].update(input_digest="f" * 64),
        lambda d: d["trace"].update(outcome="verified_robust"),
        lambda d: d.update(recorded_at="yesterday"),
    ],
)
def test_tampered_recording_is_rejected(recorded_path, corrupt):
    scenario, baseline = load()
    data = json.loads(_recorded(scenario, baseline, *GEMINI_DECISIONS).model_dump_json())
    corrupt(data)
    recorded_path.write_text(json.dumps(data), encoding="utf-8")
    doc = client.get("/v1/showcase").json()
    assert doc["recorded_ai_run_status"] == "rejected"
    assert doc["recorded_ai_run"] is None


def test_unreadable_recording_is_rejected(recorded_path):
    recorded_path.write_bytes(b"\xff not json")
    assert client.get("/v1/showcase").json()["recorded_ai_run_status"] == "rejected"


def test_public_security_headers():
    response = client.get("/")
    assert response.status_code == 200
    assert "EvacWeave" in response.text
    assert "現実の安全や避難判断を保証しません" in response.text
    assert response.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert response.headers["x-content-type-options"] == "nosniff"
    for name in ("app.css", "app.js"):
        assert client.get("/assets/" + name).status_code == 200


def test_public_request_rejects_oversized_and_duplicate_json(monkeypatch):
    monkeypatch.setenv("PUBLIC_SIMULATION_ENABLED", "1")
    assert (
        client.post(
            "/v1/simulate",
            content=b"x" * (MAX_PUBLIC_BODY + 1),
            headers={"content-type": "application/json"},
        ).status_code
        == 413
    )
    assert (
        client.post(
            "/v1/simulate",
            content=b'{"scenario":{},"scenario":{}}',
            headers={"content-type": "application/json"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/v1/simulate",
            content=b"{}",
            headers={"content-type": "text/plain"},
        ).status_code
        == 415
    )


def test_public_deeply_nested_json_fails_closed(monkeypatch):
    monkeypatch.setenv("PUBLIC_SIMULATION_ENABLED", "1")
    body = b'{"scenario":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
    response = client.post(
        "/v1/simulate",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_public_valid_simulation_matches_golden(monkeypatch):
    monkeypatch.setenv("PUBLIC_SIMULATION_ENABLED", "1")
    scenario, plan = load()
    response = client.post(
        "/v1/simulate",
        json={
            "scenario": scenario.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200
    assert response.json()["input_digest"] == simulate(scenario, plan).input_digest


def test_anonymous_custom_simulation_disabled_by_default(monkeypatch):
    monkeypatch.delenv("PUBLIC_SIMULATION_ENABLED", raising=False)
    scenario, plan = load()
    response = client.post(
        "/v1/simulate",
        json={
            "scenario": scenario.model_dump(mode="json"),
            "plan": plan.model_dump(mode="json"),
        },
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "custom simulations are disabled on public service"
    assert client.get("/v1/showcase").status_code == 200


def test_showcase_renders_a_hand_over_when_a_weakness_has_no_fallback(tmp_path, monkeypatch):
    # Shelter A is closed to new arrivals, so losing road_b1/road_b2 has no fallback.
    data = json.loads((DATA / "scenario.json").read_text())
    data["shelters"][0]["capacity"] = 0
    for name in ("plan.json", "repaired_plan.json"):
        (tmp_path / name).write_text((DATA / name).read_text())
    (tmp_path / "scenario.json").write_text(json.dumps(data))
    monkeypatch.setattr(api, "DEMO_DIR", tmp_path)
    api._showcase.cache_clear()
    try:
        doc = client.get("/v1/showcase").json()
    finally:
        api._showcase.cache_clear()
    assert doc["agent_trace"]["outcome"] == "escalated"
    assert doc["repaired_result"]["status"] == "PASS"
    assert {c["reason"] for c in doc["contingencies"]} == {"no_verified_plan"}


def test_showcase_without_any_verified_plan_is_an_error(tmp_path, monkeypatch):
    data = json.loads((DATA / "scenario.json").read_text())
    for road in data["roads"]:
        road["closures"] = [{"start_s": 0, "end_s": data["deadline_s"]}]
    (tmp_path / "plan.json").write_text((DATA / "plan.json").read_text())
    (tmp_path / "scenario.json").write_text(json.dumps(data))
    monkeypatch.setattr(api, "DEMO_DIR", tmp_path)
    api._showcase.cache_clear()
    try:
        with pytest.raises(RuntimeError, match="no verified plan"):
            api._showcase()
    finally:
        api._showcase.cache_clear()


def test_river_district_agent_covers_what_it_can_and_hands_over_the_rest():
    doc = client.get("/v1/showcase").json()
    (district,) = doc["more_runs"]
    trace = AgentTrace.model_validate_json(json.dumps(district["agent_trace"]))
    assert district["key"] == "river_district" and len(district["scenario"]["personas"]) == 12
    # The naive plan fails on a flooded road and an overfull school.
    assert set(trace.steps[0].observation.rejection_reasons) == {"route_blocked", "shelter_full"}
    assert [s.decision.action for s in trace.steps] == [
        "run_repair",
        "run_redteam",
        "plan_contingency",
        "escalate",
    ]
    assert trace.final_result.status == "PASS" and trace.outcome == "escalated"
    contingencies = trace.steps[2].effect.contingencies
    covered = {c.road_id for c in contingencies if c.covered}
    assert covered == {"center_to_hill", "east_direct", "hall_access", "school_access"}
    # No fallback exists in this network for these roads: the agent says so.
    assert {c.road_id for c in contingencies if not c.covered} == {
        "hill_road",
        "north_exit",
        "river_bridge",
        "river_path",
    }
    scenario = Scenario.model_validate_json(json.dumps(district["scenario"]))
    assert simulate(scenario, trace.final_plan) == trace.final_result
