"""The care reading: Gemini reads residents' notes into shelter restrictions.

No network and no model call: the transport and the private service are faked.
"""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import apps.api.live_agent as live
import apps.orchestrator_service.main as service
from apps.api.main import app
from packages.agents.care_reader import GeminiCareReader, care_prompt, care_schema, parse_care
from packages.agents.loop import PolicyOutputError, run_agent
from packages.domain.ai import AgentRunRequest
from packages.domain.care import CareReading, CareReadRequest, CareSet
from packages.domain.io import parse_contract
from packages.services.budget import AttemptBudget
from tests.test_live_agent import _ScriptedGemini
from tests.test_report_reader import Scripted, logs  # noqa: F401 - the fixture is used by name

ROOT = Path(__file__).resolve().parents[1]
CARE = CareSet.model_validate_json(
    (ROOT / "data/demo_area/river_district/care.json").read_text(encoding="utf-8")
)
ACTORS = tuple(r.actor_id for r in CARE.residents)
SHELTERS = tuple(s.id for s in CARE.shelters)
client = TestClient(app)


class _Transport:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def _request():
    return CareReadRequest(residents=CARE.residents, shelters=CARE.shelters)


def _reviewed():
    return {"restrictions": [r.model_dump() for r in CARE.reviewed_restrictions]}


def test_the_reader_sends_notes_as_data_with_an_enum_schema_and_one_call():
    transport = _Transport(_reviewed())
    logged = []
    reader = GeminiCareReader(
        model="gemini-3.5-flash", transport=transport, budget=AttemptBudget(0, 1), on_call=logged.append
    )
    restrictions = asyncio.run(reader.read(_request()))
    assert [(c.purpose, c.error_type) for c in logged] == [("care_read", None)]
    assert set(restrictions) == set(CARE.reviewed_restrictions)
    assert reader.model_calls == 1 and len(transport.calls) == 1
    call = transport.calls[0]
    sent = json.loads(call["prompt"])
    assert [r["id"] for r in sent["residents"]] == list(ACTORS)
    schema = call["response_schema"].model_json_schema()
    item = schema["$defs"]["Restriction"]["properties"]
    assert item["actor_id"]["enum"] == list(ACTORS)
    assert item["shelter_id"]["enum"] == list(SHELTERS)
    assert "instructions" in call["system_instruction"] and "Never infer a need" in call["system_instruction"]


@pytest.mark.parametrize(
    "answer",
    [
        {"restrictions": [{"actor_id": "mallory", "shelter_id": "school_shelter", "reason": "night_safety"}]},
        {"restrictions": [{"actor_id": "north_01", "shelter_id": "elsewhere", "reason": "night_safety"}]},
        {"restrictions": [{"actor_id": "north_01", "shelter_id": "school_shelter", "reason": "dislikes_it"}]},
        {"restrictions": [{"actor_id": "north_01", "shelter_id": "school_shelter"}]},
        {"restrictions": "all of them"},
        {},
    ],
)
def test_anything_not_offered_is_refused(answer):
    with pytest.raises(PolicyOutputError):
        parse_care(answer, ACTORS, SHELTERS)


def test_a_repeated_pair_keeps_the_first_reason():
    twice = {
        "restrictions": [
            {"actor_id": "north_01", "shelter_id": "hill_shelter", "reason": "step_free_access"},
            {"actor_id": "north_01", "shelter_id": "hill_shelter", "reason": "night_safety"},
        ]
    }
    (only,) = parse_care(twice, ACTORS, SHELTERS)
    assert only.reason == "step_free_access"


def test_the_schema_puts_no_length_limit_on_the_list_and_the_parser_bounds_it():
    # Vertex AI refused the care request (HTTP 400, before any answer) while this list had
    # an item limit of 36; long limits are a documented cause. The answer is bounded here.
    restrictions = care_schema(ACTORS, SHELTERS).model_json_schema()["properties"]["restrictions"]
    assert "maxItems" not in restrictions and "minItems" not in restrictions
    pairs = [(a, s) for a in ACTORS for s in SHELTERS]
    every = [{"actor_id": a, "shelter_id": s, "reason": "night_safety"} for a, s in pairs]
    assert len(parse_care({"restrictions": every}, ACTORS, SHELTERS)) == len(pairs)
    with pytest.raises(PolicyOutputError):
        parse_care({"restrictions": every + every[:1]}, ACTORS, SHELTERS)


def test_the_prompt_carries_notes_and_facilities_only():
    sent = json.loads(care_prompt(_request()))
    assert set(sent) == {"residents", "shelters"}
    assert sent["shelters"][0]["facilities"] == CARE.shelters[0].facilities


def test_the_reviewed_notes_leave_a_plan_that_keeps_every_restriction():
    response = client.post(
        "/v1/agent/challenge",
        json={
            "scenario": "river_district",
            "restrictions": [r.model_dump() for r in CARE.reviewed_restrictions],
        },
    )
    assert response.status_code == 200, response.text
    trace = response.json()["trace"]
    assert trace["initial_result"]["status"] == "FAIL"
    assert any(e["reason"] == "unsuitable_shelter" for e in trace["initial_result"]["events"])
    assert trace["final_result"]["status"] == "PASS"
    chosen = {(i["actor_id"], i["shelter_id"]) for i in trace["final_plan"]["intents"]}
    assert not chosen & {(r.actor_id, r.shelter_id) for r in CARE.reviewed_restrictions}


def test_care_notes_are_served_for_the_district_only():
    # The district now has a pool of residents: pattern 0, served first, is its own twelve.
    served = CareSet.model_validate_json(client.get("/v1/agent/care").text)
    assert (served.residents, served.shelters) == (CARE.residents, CARE.shelters)
    assert set(served.reviewed_restrictions) == set(CARE.reviewed_restrictions)
    assert (served.pattern, served.patterns) == (0, 30)
    assert client.get("/v1/agent/care?scenario=base").status_code == 404


@pytest.fixture
def live_care(monkeypatch):
    monkeypatch.setenv("LIVE_AI_ENABLED", "1")
    monkeypatch.setenv("ORCHESTRATOR_URL", "https://disaster-agentic-orchestrator-abc123-an.a.run.app")
    monkeypatch.setenv("LIVE_AI_DAILY_RUNS", "3")
    monkeypatch.delenv("K_SERVICE", raising=False)
    monkeypatch.setattr(live, "QUOTA", live.DailyQuota())
    sent = []

    def fake_care(origin, body, trace_header):
        request = parse_contract(body, CareReadRequest)
        sent.append(request)
        return json.dumps({"run_id": "d" * 32, "model": "gemini-3.5-flash", **_reviewed()}).encode()

    monkeypatch.setattr(live, "_post_care", fake_care)
    return sent


def test_a_visitor_rewrites_a_note_and_gemini_reads_all_of_them(live_care):
    response = client.post(
        "/v1/agent/care",
        json={
            "scenario": "river_district",
            "notes": [{"actor_id": "north_04", "note": "最近足を骨折して松葉杖を使っている"}],
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["runs_left_today"] == 2 and len(body["reading"]["restrictions"]) == 12
    (request,) = live_care
    notes = {r.actor_id: r.note for r in request.residents}
    assert notes["north_04"] == "最近足を骨折して松葉杖を使っている"
    assert notes["north_01"] == CARE.residents[0].note


@pytest.mark.parametrize(
    "notes",
    [
        [{"actor_id": "nobody", "note": "x"}],
        [{"actor_id": "north_01", "note": "a"}, {"actor_id": "north_01", "note": "b"}],
        [{"actor_id": "north_01", "note": "line one\nline two"}],
        [{"actor_id": "north_01", "note": "長" * 101}],
    ],
)
def test_only_known_residents_and_one_line_notes_are_read(live_care, notes):
    assert (
        client.post("/v1/agent/care", json={"scenario": "river_district", "notes": notes}).status_code == 422
    )
    assert live_care == []


def test_a_reading_naming_someone_else_is_refused(live_care, monkeypatch):
    def stranger(origin, body, trace_header):
        bad = {"actor_id": "east_09", "shelter_id": "hill_shelter", "reason": "night_safety"}
        return json.dumps({"run_id": "d" * 32, "model": "gemini-3.5-flash", "restrictions": [bad]}).encode()

    monkeypatch.setattr(live, "_post_care", stranger)
    response = client.post("/v1/agent/care", json={"scenario": "river_district"})
    assert response.status_code == 502


def test_a_live_run_carries_the_restrictions_to_the_private_service(live_care, monkeypatch):
    seen = []

    def fake_run(origin, body, trace_header):
        request = parse_contract(body, AgentRunRequest)
        seen.append(request.scenario)
        trace = asyncio.run(run_agent(request.scenario, request.plan, _ScriptedGemini(), run_id="e" * 32))
        return trace.model_dump_json().encode()

    monkeypatch.setattr(live, "_post_run", fake_run)
    restrictions = [r.model_dump() for r in CARE.reviewed_restrictions]
    response = client.post(
        "/v1/agent/live", json={"scenario": "river_district", "restrictions": restrictions}
    )
    assert response.status_code == 200, response.text
    assert set(seen[0].restrictions) == set(CARE.reviewed_restrictions)
    assert response.json()["trace"]["final_result"]["status"] == "PASS"


# --- the private service's /v1/care/read --------------------------------------------------


client_private = TestClient(service.app)


def _use_care(monkeypatch, transport):
    reader = GeminiCareReader(model="gemini-3.5-flash", transport=transport, budget=AttemptBudget(0, 1))
    monkeypatch.setattr(service, "_make_care_reader", lambda: reader)
    return reader


def test_private_care_read_returns_restrictions_and_logs_only_counts(monkeypatch, logs):  # noqa: F811
    _use_care(monkeypatch, Scripted(_reviewed()))
    response = client_private.post("/v1/care/read", json=json.loads(_request().model_dump_json()))
    assert response.status_code == 200, response.text
    reading = parse_contract(response.content, CareReading)
    assert set(reading.restrictions) == set(CARE.reviewed_restrictions)
    entry = next(line for line in logs if line["message"] == "care_read")
    assert (entry["residents"], entry["shelters"], entry["restrictions"]) == (12, 3, 12)
    text = json.dumps(logs, ensure_ascii=False)
    assert "車椅子" not in text and "DV" not in text


def test_private_care_read_refuses_a_malformed_answer(monkeypatch, logs):  # noqa: F811
    _use_care(monkeypatch, Scripted({"restrictions": [{"actor_id": "x", "shelter_id": "y", "reason": "z"}]}))
    response = client_private.post("/v1/care/read", json=json.loads(_request().model_dump_json()))
    assert response.status_code == 502
    assert any(line["message"] == "care_read_invalid_output" for line in logs)


def test_the_prompt_describes_every_reason_and_forbids_guessing_from_who_someone_is():
    from packages.agents.care_reader import PROMPT_VERSION, REASONS
    from packages.prompts.loader import load_prompt

    prompt = load_prompt("care_reader", "v2")
    assert all(f"- {reason}:" in prompt for reason in REASONS) and len(REASONS) == 9
    assert "Never infer a need from a name, age, gender, nationality, origin, religion or language" in prompt
    assert PROMPT_VERSION == "care_reader/v2" == CareReading.model_fields["prompt_version"].default


def test_a_care_reading_gets_its_own_longer_time_budget(monkeypatch):
    # It answers for every resident at once (18 s in production), so 25 s left too little room.
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", service.ALLOWED_PROJECT)
    reader = service._make_care_reader()  # builds the client lazily: nothing is sent
    assert reader.timeout_s == service.CARE_CALL_TIMEOUT_S > service.MODEL_CALL_TIMEOUT_S


def test_a_care_reading_that_hangs_is_a_504(monkeypatch):
    class Hanging:
        async def generate(self, **kwargs):
            await asyncio.sleep(5)

    _use_care(monkeypatch, Hanging())
    monkeypatch.setattr(service, "CARE_TIMEOUT_S", 0.05)
    response = client_private.post("/v1/care/read", json=json.loads(_request().model_dump_json()))
    assert response.status_code == 504
