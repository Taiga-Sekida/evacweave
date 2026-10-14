"""Private agent service: Gemini as policy, with fakes only (no network, no paid call)."""

import json
import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import apps.orchestrator_service.main as service
from packages.agents.orchestrator import GeminiAgentPolicy
from packages.domain.agent import AgentTrace
from packages.services.budget import AttemptBudget
from packages.services.structured_log import CloudLoggingFormatter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "demo_area" / "blocked_route"

client = TestClient(service.app)


def _payload(plan_name: str = "plan.json") -> dict:
    return {
        "schema_version": "agent-run-request/v1",
        "scenario": json.loads((FIXTURE / "scenario.json").read_text(encoding="utf-8")),
        "plan": json.loads((FIXTURE / plan_name).read_text(encoding="utf-8")),
    }


class ScriptedTransport:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    async def generate(self, **kwargs):
        self.prompts.append(json.loads(kwargs["prompt"]))
        action, reason = self.answers.pop(0)
        return {"action": action, "reason_code": reason}


def _use(monkeypatch, transport, calls=service.LIMITS.max_model_calls):
    policy = GeminiAgentPolicy(
        service.DEFAULT_MODEL, transport, AttemptBudget(gemma_limit=0, gemini_limit=calls)
    )
    monkeypatch.setattr(service, "_make_policy", lambda: policy)
    return policy


class _Collect(logging.Handler):
    def __init__(self):
        super().__init__()
        self.setFormatter(CloudLoggingFormatter())
        self.lines = []

    def emit(self, record):
        self.lines.append(json.loads(self.format(record)))


@pytest.fixture
def logs():
    handler = _Collect()
    service.logger.addHandler(handler)
    yield handler.lines
    service.logger.removeHandler(handler)


def test_gemini_drives_the_loop_and_the_core_verifies(monkeypatch, logs):
    transport = ScriptedTransport(
        ("run_repair", "plan_failed_verification"),
        ("run_redteam", "check_robustness"),
        ("plan_contingency", "cover_single_points_of_failure"),
        ("finish", "verified_plan_ready"),
    )
    _use(monkeypatch, transport)
    response = client.post(
        "/v1/agent/run",
        json=_payload(),
        headers={"X-Cloud-Trace-Context": "0123456789abcdef0123456789abcdef/1;o=1"},
    )
    assert response.status_code == 200
    trace = AgentTrace.model_validate_json(response.text)
    assert trace.policy == "vertex-gemini"
    assert trace.model == service.DEFAULT_MODEL
    assert trace.prompt_version == "orchestrator/v2"
    assert trace.model_calls == 4
    assert trace.outcome == "verified_with_contingencies"
    assert transport.prompts[0]["observation"]["plan_status"] == "FAIL"

    steps = [entry for entry in logs if entry["message"] == "agent_step"]
    assert [s["action"] for s in steps] == ["run_repair", "run_redteam", "plan_contingency", "finish"]
    summary = next(entry for entry in logs if entry["message"] == "agent_run")
    assert summary["outcome"] == "verified_with_contingencies"
    assert summary["logging.googleapis.com/trace"] == (
        "projects/physical-ai-guardian/traces/0123456789abcdef0123456789abcdef"
    )
    assert all(entry["severity"] == "INFO" for entry in steps)


def test_gemini_cannot_finish_a_failed_plan(monkeypatch):
    _use(monkeypatch, ScriptedTransport(("finish", "verified_plan_ready")))
    trace = AgentTrace.model_validate_json(client.post("/v1/agent/run", json=_payload()).text)
    assert trace.outcome == "halted_policy_violation"
    assert trace.steps[0].policy_error == "action_not_allowed"
    assert trace.final_plan is None


def test_model_call_budget_halts_the_run(monkeypatch):
    _use(monkeypatch, ScriptedTransport(("run_repair", "plan_failed_verification")), calls=1)
    trace = AgentTrace.model_validate_json(client.post("/v1/agent/run", json=_payload()).text)
    assert trace.model_calls == 1
    assert trace.steps[-1].policy_error == "model_budget_exhausted"
    assert trace.outcome == "halted_model_error"


def test_run_timeout_returns_504(monkeypatch):
    class Hanging:
        async def generate(self, **kwargs):
            import asyncio

            await asyncio.sleep(5)

    _use(monkeypatch, Hanging())
    monkeypatch.setattr(service, "RUN_TIMEOUT_S", 0.05)
    response = client.post("/v1/agent/run", json=_payload())
    assert response.status_code == 504


def test_misconfiguration_returns_503_without_calling_a_model(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-project")
    response = client.post("/v1/agent/run", json=_payload())
    assert response.status_code == 503
    assert response.json() == {"detail": "agent unavailable"}


def test_callers_cannot_inject_a_verdict():
    payload = _payload()
    payload["simulation"] = {"status": "PASS"}
    assert client.post("/v1/agent/run", json=payload).status_code == 422


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        (b'{"schema_version":"agent-run-request/v1","schema_version":"agent-run-request/v1"}', 422),
        (b"{" + b" " * service.MAX_REQUEST_BYTES + b"}", 413),
    ],
)
def test_strict_and_bounded_input(raw, status):
    response = client.post("/v1/agent/run", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == status


def test_non_json_content_type_is_rejected():
    response = client.post("/v1/agent/run", content=b"{}", headers={"content-type": "text/plain"})
    assert response.status_code == 415


def test_streamed_request_enforces_limit_without_content_length():
    def chunks():
        yield b"{"
        yield b" " * service.MAX_REQUEST_BYTES

    response = client.post(
        "/v1/agent/run",
        content=chunks(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 413


def test_exact_body_limit_is_not_rejected_as_too_large():
    body = b"{}" + b" " * (service.MAX_REQUEST_BYTES - 2)
    response = client.post(
        "/v1/agent/run",
        content=body,
        headers={"content-type": "application/json"},
    )
    # The document lacks required contract fields, but is within the size budget.
    assert response.status_code == 422


def test_deeply_nested_json_fails_closed():
    body = b'{"scenario":' + b"[" * 1500 + b"0" + b"]" * 1500 + b"}"
    response = client.post(
        "/v1/agent/run",
        content=body,
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_model_allowlist_and_project_lock(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "other-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", service.DEFAULT_MODEL)
    with pytest.raises(RuntimeError, match="PROJECT"):
        service._settings()

    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", "arbitrary-model")
    with pytest.raises(RuntimeError, match="allowlisted"):
        service._settings()


def test_default_model_is_allowlisted_and_retired_model_is_rejected(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("VERTEX_GEMINI_MODEL", raising=False)
    assert service._settings()[2] == service.DEFAULT_MODEL
    assert service.DEFAULT_MODEL in service.ALLOWED_MODELS

    # gemini-2.5-flash retires on Vertex AI on 2026-10-20, inside the judging window.
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", "gemini-2.5-flash")
    with pytest.raises(RuntimeError, match="allowlisted"):
        service._settings()


def test_health_reports_configuration(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.delenv("VERTEX_GEMINI_MODEL", raising=False)
    body = client.get("/health").json()
    assert (body["config"], body["model"]) == ("ready", service.DEFAULT_MODEL)
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", "gemini-2.5-flash")
    assert client.get("/health").json()["config"] == "misconfigured"


def test_limits_fit_inside_the_cloud_run_timeout():
    # The full chain, down to the deploy scripts, is checked in test_gemini_calls.py.
    assert service.RUN_TIMEOUT_S < service.CLOUD_RUN_TIMEOUT_S
    assert service.LIMITS.max_model_calls >= service.LIMITS.max_steps


def test_trace_carries_the_run_id_seen_in_the_logs(monkeypatch, logs):
    _use(monkeypatch, ScriptedTransport(("escalate", "needs_human_judgement")))
    trace = AgentTrace.model_validate_json(client.post("/v1/agent/run", json=_payload()).text)
    assert trace.run_id is not None
    assert {entry["run_id"] for entry in logs} == {trace.run_id}


def test_unexpected_failure_is_a_500_logged_by_type_only(monkeypatch, logs):
    _use(monkeypatch, ScriptedTransport())

    async def explode(*args, **kwargs):
        raise ValueError("caller input: resident_01 secret")

    monkeypatch.setattr(service, "run_agent", explode)
    response = client.post("/v1/agent/run", json=_payload())
    assert response.status_code == 500
    assert response.json() == {"detail": "agent failed"}
    failure = next(entry for entry in logs if entry["message"] == "agent_failed")
    assert failure["error_type"] == "ValueError"
    assert "secret" not in json.dumps(logs)


def test_waiting_for_a_slot_counts_against_the_deadline(monkeypatch):
    import asyncio

    _use(monkeypatch, ScriptedTransport())
    monkeypatch.setattr(service, "AI_CONCURRENCY", asyncio.Semaphore(0))  # never free
    monkeypatch.setattr(service, "RUN_TIMEOUT_S", 0.05)
    assert client.post("/v1/agent/run", json=_payload()).status_code == 504


def test_deeply_nested_json_is_a_422_not_a_crash():
    raw = b"[" * 60_000 + b"]" * 60_000
    response = client.post("/v1/agent/run", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == 422


@pytest.mark.parametrize(
    ("length", "status"), [("abc", 400), ("-1", 400), (str(10**9), 413), ("9" * 5000, 413)]
)
def test_declared_content_length_is_checked_before_reading(length, status):
    response = client.post(
        "/v1/agent/run",
        content=b"{}",
        headers={"content-type": "application/json", "content-length": length},
    )
    assert response.status_code == status


@pytest.mark.parametrize(
    ("location", "config"),
    [("us-central1", "ready"), ("asia-northeast1", "ready"), ("europe-west4", "misconfigured")],
)
def test_vertex_endpoint_is_switchable_within_an_allowlist(monkeypatch, location, config):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", location)
    monkeypatch.delenv("VERTEX_GEMINI_MODEL", raising=False)
    body = client.get("/health").json()
    assert body["config"] == config
