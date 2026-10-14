"""Gemini requests: logging, timeouts, error classes and the one-call diagnostic.

Fakes only: no network, no SDK, no paid call.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import apps.api.live_agent as live
import apps.orchestrator_service.main as service
from packages.agents.loop import ReplayPolicy, run_agent
from packages.agents.orchestrator import (
    MIN_CALL_S,
    GeminiAgentPolicy,
    GoogleGenAITransport,
    ModelCall,
    api_error_hint,
    api_status_code,
    call_timeout,
)
from packages.agents.report_reader import GeminiReportReader
from packages.domain.agent import AgentTrace
from packages.domain.diagnostics import GeminiDiagnostic
from packages.domain.io import load_contract
from packages.domain.models import Plan, Scenario
from packages.services.budget import AttemptBudget, BudgetExceeded
from packages.services.structured_log import CloudLoggingFormatter

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "demo_area" / "blocked_route"
SECRET = "SECRET-PROMPT-OR-BODY"
client = TestClient(service.app)


class FakeAPIError(Exception):
    """Shaped like google.genai.errors.APIError: an HTTP code and a message holding the body."""

    def __init__(self, code: int) -> None:
        super().__init__(f"{code} response body: {SECRET}")
        self.code = code


class Transport:
    """Answers, raises or hangs, call by call; records what it was asked."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls = []
        self.prepared = 0

    async def prepare(self) -> None:
        self.prepared += 1

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        answer = self.answers.pop(0)
        if answer == "hang":
            await asyncio.sleep(30)
        if isinstance(answer, BaseException):
            raise answer
        return answer


REPAIR = {"action": "run_repair", "reason_code": "plan_failed_verification"}


def _inputs():
    return load_contract(FIXTURE / "scenario.json", Scenario), load_contract(FIXTURE / "plan.json", Plan)


def _payload():
    scenario, plan = _inputs()
    return {
        "schema_version": "agent-run-request/v1",
        "scenario": scenario.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
    }


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


def _use(monkeypatch, transport, *, timeout_s=service.MODEL_CALL_TIMEOUT_S):
    policy = GeminiAgentPolicy(
        service.DEFAULT_MODEL,
        transport,
        AttemptBudget(gemma_limit=0, gemini_limit=service.LIMITS.max_model_calls),
        timeout_s=timeout_s,
    )
    monkeypatch.setattr(service, "_make_policy", lambda: policy)
    return policy


# --- what a failed call leaves in the logs ------------------------------------------------


def test_a_failed_call_logs_only_its_class_status_time_model_and_run_id(monkeypatch, logs):
    _use(monkeypatch, Transport(FakeAPIError(403)))
    trace = AgentTrace.model_validate_json(client.post("/v1/agent/run", json=_payload()).text)
    assert trace.outcome == "halted_model_error" and trace.steps[0].policy_error == "model_error"
    [failed] = [line for line in logs if line["message"] == "gemini_call_failed"]
    assert set(failed) == {
        "severity",
        "message",
        "logger",
        "run_id",
        "model",
        "purpose",
        "elapsed_ms",
        "error_type",
        "status_code",
    }
    assert (failed["run_id"], failed["model"], failed["purpose"]) == (
        trace.run_id,
        service.DEFAULT_MODEL,
        "decision",
    )
    assert (failed["error_type"], failed["status_code"], failed["severity"]) == (
        "FakeAPIError",
        403,
        "WARNING",
    )
    assert isinstance(failed["elapsed_ms"], int)
    assert SECRET not in json.dumps(logs)


def test_a_timed_out_call_is_logged_as_a_timeout_and_halts_the_run(monkeypatch, logs):
    _use(monkeypatch, Transport("hang"), timeout_s=0.05)
    trace = AgentTrace.model_validate_json(client.post("/v1/agent/run", json=_payload()).text)
    assert (trace.outcome, trace.model_calls, trace.steps[0].policy_error) == (
        "halted_model_error",
        1,
        "model_error",
    )
    [failed] = [line for line in logs if line["message"] == "gemini_call_failed"]
    assert (failed["error_type"], failed["status_code"]) == ("TimeoutError", None)
    assert failed["elapsed_ms"] >= 50


def test_successful_calls_are_logged_with_their_time(monkeypatch, logs):
    _use(monkeypatch, Transport(REPAIR, {"action": "escalate", "reason_code": "needs_human_judgement"}))
    client.post("/v1/agent/run", json=_payload())
    ok = [line for line in logs if line["message"] == "gemini_call"]
    assert len(ok) == 2 and all("error_type" not in line for line in ok)


def test_the_client_is_prepared_before_the_first_call_and_outside_its_timer(monkeypatch):
    transport = Transport(REPAIR, {"action": "escalate", "reason_code": "needs_human_judgement"})
    _use(monkeypatch, transport)
    client.post("/v1/agent/run", json=_payload())
    assert transport.prepared == 1


def test_a_client_that_cannot_be_prepared_is_a_503_without_a_call(monkeypatch, logs):
    transport = Transport()

    async def broken():
        raise FakeAPIError(401)

    transport.prepare = broken
    _use(monkeypatch, transport)
    response = client.post("/v1/agent/run", json=_payload())
    assert response.status_code == 503
    assert transport.calls == []
    [failed] = [line for line in logs if line["message"] == "gemini_client_failed"]
    assert (failed["error_type"], failed["status_code"]) == ("FakeAPIError", 401)
    assert SECRET not in json.dumps(logs)


def test_the_reader_logs_its_call_too(monkeypatch, logs):
    reader = GeminiReportReader(
        service.DEFAULT_MODEL, Transport(FakeAPIError(429)), AttemptBudget(gemma_limit=0, gemini_limit=1)
    )
    monkeypatch.setattr(service, "_make_report_reader", lambda: reader)
    body = {
        "report": "橋が冠水",
        "roads": [{"id": "river_bridge", "name": "橋", "between": ["橋のたもと", "中央"]}],
    }
    assert client.post("/v1/report/read", json=body).status_code == 500
    [failed] = [line for line in logs if line["message"] == "gemini_call_failed"]
    assert (failed["purpose"], failed["status_code"]) == ("report_read", 429)
    assert "冠水" not in json.dumps(logs, ensure_ascii=False)


# --- error classes ------------------------------------------------------------------------


class _Response:
    status_code = 504


class _HttpxLike(Exception):
    response = _Response()


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (FakeAPIError(404), 404),
        (_HttpxLike(), 504),
        (TimeoutError(), None),
        (RuntimeError("x"), None),
        (type("Weird", (Exception,), {"code": True})(), None),  # a bool is not a status
        (type("Odd", (Exception,), {"code": 42})(), None),  # nor an out-of-range int
    ],
)
def test_api_status_code_reads_only_real_http_statuses(exc, code):
    assert api_status_code(exc) == code


class _Refused(FakeAPIError):
    def __init__(self, detail: str) -> None:
        super().__init__(400)
        self.args = (f"400 INVALID_ARGUMENT. {detail} {SECRET}",)


@pytest.mark.parametrize(
    ("exc", "hint"),
    [
        (
            _Refused("The specified schema produces a constraint that has too many states"),
            "schema_too_complex",
        ),
        (_Refused("Invalid JSON payload: response_schema"), "schema_refused"),
        (_Refused("thinking_level is not supported"), "thinking_refused"),
        (_Refused("max_output_tokens is out of range"), "token_limit_refused"),
        (_Refused("Request contains an invalid argument."), "other"),
        (FakeAPIError(429), None),
        (TimeoutError(), None),
    ],
)
def test_a_refused_request_is_named_by_fixed_words_only(exc, hint):
    assert api_error_hint(exc) == hint


def test_the_refusal_hint_is_logged_but_not_the_message(monkeypatch, logs):
    _use(monkeypatch, Transport(_Refused("schema has too many states")))
    client.post("/v1/agent/run", json=_payload())
    [failed] = [line for line in logs if line["message"] == "gemini_call_failed"]
    assert (failed["status_code"], failed["error_hint"]) == (400, "schema_too_complex")
    assert SECRET not in json.dumps(logs) and "too many" not in json.dumps(logs)


# --- time budget --------------------------------------------------------------------------


def test_a_call_is_cut_to_the_time_left_before_the_deadline(monkeypatch):
    import packages.agents.orchestrator as orchestrator

    monkeypatch.setattr(orchestrator.time, "monotonic", lambda: 100.0)
    assert call_timeout(25.0, None) == 25.0
    assert call_timeout(25.0, 200.0) == 25.0
    assert call_timeout(25.0, 110.0) == 10.0
    with pytest.raises(BudgetExceeded):
        call_timeout(25.0, 100.0 + MIN_CALL_S - 0.1)


def test_out_of_time_sends_no_call_and_counts_none_and_replays(monkeypatch):
    transport = Transport()
    scenario, plan = _inputs()
    policy = GeminiAgentPolicy(
        "gemini-3.5-flash", transport, AttemptBudget(gemma_limit=0, gemini_limit=6), deadline=0.0
    )
    trace = asyncio.run(run_agent(scenario, plan, policy, run_id="f" * 32))
    assert transport.calls == []
    assert (trace.model_calls, trace.outcome) == (0, "halted_model_error")
    assert trace.steps[0].policy_error == "model_budget_exhausted"
    # The public service replays the trace before showing it; the recount agrees.
    replayed = asyncio.run(
        run_agent(scenario, plan, ReplayPolicy(trace), limits=trace.limits, run_id=trace.run_id)
    )
    assert replayed == trace


def test_each_call_gets_the_sdk_timeout_it_is_held_to(monkeypatch):
    transport = Transport(REPAIR)
    policy = GeminiAgentPolicy(
        "gemini-3.5-flash", transport, AttemptBudget(gemma_limit=0, gemini_limit=1), timeout_s=7.5
    )
    scenario, plan = _inputs()
    asyncio.run(run_agent(scenario, plan, policy))
    assert transport.calls[0]["timeout_s"] == 7.5


def _deploy_timeout(script: str, pattern: str) -> int:
    match = re.search(pattern, (ROOT / "scripts" / script).read_text(encoding="utf-8"))
    assert match, script
    return int(match.group(1))


def test_timeouts_nest_from_one_call_to_the_public_request():
    private = _deploy_timeout("deploy_cloud_run_orchestrator.sh", r"--timeout (\d+)\b")
    public = _deploy_timeout("deploy_cloud_run_mvp.sh", r"--timeout (\d+)s\b")
    assert private == service.CLOUD_RUN_TIMEOUT_S
    assert public == live.PUBLIC_CLOUD_RUN_TIMEOUT_S
    # Private service: a call fits the run's model deadline, which fits the backstop,
    # which fits Cloud Run's own timeout (so the trace, not a gateway error, comes back).
    assert service.MODEL_CALL_TIMEOUT_S <= service.RUN_MODEL_DEADLINE_S < service.RUN_TIMEOUT_S < private
    assert service.MODEL_CALL_TIMEOUT_S <= service.READ_MODEL_DEADLINE_S < service.READ_TIMEOUT_S < private
    assert service.CARE_CALL_TIMEOUT_S <= service.CARE_MODEL_DEADLINE_S < service.CARE_TIMEOUT_S < private
    assert service.DIAGNOSTIC_CALL_TIMEOUT_S <= service.DIAGNOSTIC_DEADLINE_S < private
    # Public service: waits longer than the private one can take, and less than its own limit.
    assert private < live.LIVE_RUN_TIMEOUT_S and public > live.LIVE_RUN_TIMEOUT_S + 3
    assert service.READ_TIMEOUT_S < live.LIVE_READ_TIMEOUT_S and public > live.LIVE_READ_TIMEOUT_S + 3
    assert service.CARE_TIMEOUT_S < live.LIVE_CARE_TIMEOUT_S and public > live.LIVE_CARE_TIMEOUT_S + 3


def test_apply_limits_matches_the_deploy_scripts():
    limits = (ROOT / "scripts" / "apply_cloud_run_limits.sh").read_text(encoding="utf-8")
    assert f"--timeout={live.PUBLIC_CLOUD_RUN_TIMEOUT_S}s" in limits
    assert f"--timeout={service.CLOUD_RUN_TIMEOUT_S}s" in limits


def test_the_transport_builds_its_client_once(monkeypatch):
    transport = GoogleGenAITransport(project="p")
    built = []

    def build():
        built.append(1)
        return object()

    monkeypatch.setattr(transport, "_build_client", build)

    async def twice():
        await asyncio.gather(transport.prepare(), transport.prepare())

    asyncio.run(twice())
    assert built == [1]


def test_the_transport_loads_credentials_and_disables_retries(monkeypatch):
    import sys
    import types

    seen = {}

    def options(**kwargs):
        return types.SimpleNamespace(**kwargs)

    class Credentials:
        def refresh(self, request):
            seen["refreshed"] = True

    genai = types.ModuleType("google.genai")
    genai.types = types.SimpleNamespace(HttpOptions=options, HttpRetryOptions=options)
    genai.Client = lambda **kwargs: seen.setdefault("client", kwargs)
    auth = types.ModuleType("google.auth")
    auth.default = lambda scopes: (Credentials(), "p")
    requests_mod = types.ModuleType("google.auth.transport.requests")
    requests_mod.Request = lambda: None
    google = types.ModuleType("google")
    google.genai, google.auth = genai, auth
    for name, module in {
        "google": google,
        "google.genai": genai,
        "google.auth": auth,
        "google.auth.transport": types.ModuleType("google.auth.transport"),
        "google.auth.transport.requests": requests_mod,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)
    GoogleGenAITransport(project="p")._build_client()
    assert seen["refreshed"] is True
    built = seen["client"]
    assert built["vertexai"] is True and built["location"] == "global"
    assert built["http_options"].retry_options.attempts == 1


# --- one-call diagnostic ------------------------------------------------------------------


@pytest.fixture
def diagnosis(monkeypatch, logs):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    monkeypatch.delenv("VERTEX_GEMINI_MODEL", raising=False)
    monkeypatch.setattr(service, "DIAGNOSTICS", service.DailyCount())

    def use(*answers):
        transport = Transport(*answers)
        transport.profiles = []

        def pick(project, location, profile):
            transport.profiles.append(profile)
            return transport

        monkeypatch.setattr(service, "_transport", pick)
        return transport

    return use


def _diagnose(purpose="decision", headers=None, **extra):
    return client.post(
        "/v1/diagnostics/gemini",
        json={
            "schema_version": "gemini-diagnostic-request/v1",
            "confirm_paid_call": True,
            "purpose": purpose,
            **extra,
        },
        headers=headers,
    )


def _id_token(email):
    import base64

    claims = base64.urlsafe_b64encode(json.dumps({"email": email}).encode()).decode().rstrip("=")
    return {"Authorization": f"Bearer e30.{claims}.signature"}


def test_a_service_account_cannot_run_a_diagnostic(diagnosis):
    transport = diagnosis(REPAIR)
    public = _id_token("evacweave-public-sa@physical-ai-guardian.iam.gserviceaccount.com")
    response = _diagnose(headers=public)
    assert response.status_code == 403 and transport.calls == []
    # An operator's own token (Cloud Shell) is let through to the usual checks.
    assert _diagnose(headers=_id_token("operator@example.com")).status_code == 200
    assert service._caller_email("Bearer not-a-jwt") is None
    assert service._caller_email("Basic abc.def") is None


def test_diagnostic_makes_exactly_one_production_shaped_call(diagnosis, logs):
    transport = diagnosis(REPAIR, REPAIR)
    response = _diagnose()
    assert response.status_code == 200, response.text
    result = GeminiDiagnostic.model_validate_json(response.text)
    assert (result.ok, result.failed_stage, result.model_calls) == (True, None, 1)
    assert result.production_call_timeout_ms == int(service.MODEL_CALL_TIMEOUT_S * 1000)
    assert len(transport.calls) == 1 and transport.prepared == 1
    sent = json.loads(transport.calls[0]["prompt"])
    assert sent["observation"]["plan_status"] == "FAIL"
    assert transport.calls[0]["timeout_s"] <= service.DIAGNOSTIC_CALL_TIMEOUT_S
    entry = next(line for line in logs if line["message"] == "gemini_diagnostic")
    assert entry["run_id"] == result.run_id and entry["ok"] is True


@pytest.mark.parametrize(
    ("answer", "stage", "error", "status"),
    [
        (FakeAPIError(404), "call", "FakeAPIError", 404),
        (TimeoutError(), "call", "TimeoutError", None),
        ({"action": "finish", "reason_code": "nonsense"}, "answer", "PolicyOutputError", None),
    ],
)
def test_diagnostic_names_the_failing_stage(diagnosis, logs, answer, stage, error, status):
    diagnosis(answer)
    result = GeminiDiagnostic.model_validate_json(_diagnose().text)
    assert (result.ok, result.failed_stage, result.error_type, result.status_code) == (
        False,
        stage,
        error,
        status,
    )
    assert result.model_calls == 1 and result.call_ms is not None
    assert SECRET not in json.dumps(logs)


def test_diagnostic_reports_a_client_that_cannot_be_prepared(diagnosis):
    transport = diagnosis()

    async def broken():
        raise FakeAPIError(401)

    transport.prepare = broken
    result = GeminiDiagnostic.model_validate_json(_diagnose().text)
    assert (result.failed_stage, result.error_type, result.status_code) == ("prepare", "FakeAPIError", 401)
    assert (result.model_calls, result.call_ms) == (0, None)
    assert transport.calls == []


def test_diagnostic_can_read_a_report_once(diagnosis):
    transport = diagnosis({"closed_roads": ["river_bridge"]})
    result = GeminiDiagnostic.model_validate_json(_diagnose("report_read").text)
    assert result.ok and result.purpose == "report_read" and len(transport.calls) == 1


@pytest.mark.parametrize(
    "body",
    [
        {"schema_version": "gemini-diagnostic-request/v1"},
        {"schema_version": "gemini-diagnostic-request/v1", "confirm_paid_call": False},
        {"schema_version": "gemini-diagnostic-request/v1", "confirm_paid_call": True, "prompt": "x"},
    ],
)
def test_diagnostic_needs_an_explicit_paid_call_confirmation(diagnosis, body):
    transport = diagnosis(REPAIR)
    assert client.post("/v1/diagnostics/gemini", json=body).status_code == 422
    assert transport.calls == []


def test_diagnostics_are_capped_per_day(diagnosis):
    transport = diagnosis(*([REPAIR] * 5))
    codes = [_diagnose().status_code for _ in range(service.DIAGNOSTIC_DAILY_LIMIT + 1)]
    assert codes == [200] * service.DIAGNOSTIC_DAILY_LIMIT + [429]
    assert len(transport.calls) == service.DIAGNOSTIC_DAILY_LIMIT


def test_a_second_diagnostic_is_refused_while_one_runs(diagnosis, monkeypatch):
    transport = diagnosis(REPAIR)
    busy = asyncio.Lock()
    asyncio.run(busy.acquire())
    monkeypatch.setattr(service, "DIAGNOSTIC_RUNNING", busy)
    assert _diagnose().status_code == 429
    assert transport.calls == []


def test_misconfigured_service_diagnoses_nothing(diagnosis, monkeypatch):
    transport = diagnosis(REPAIR)
    monkeypatch.setenv("VERTEX_GEMINI_MODEL", "gemini-2.5-flash")
    assert _diagnose().status_code == 503
    assert transport.calls == []


def test_model_call_record_is_plain_data():
    call = ModelCall("decision", "m", 12, "TimeoutError", None)
    assert (call.purpose, call.elapsed_ms, call.error_type) == ("decision", 12, "TimeoutError")


# --- request profiles ---------------------------------------------------------------------


def test_a_decision_diagnostic_uses_the_decision_profile_unless_it_names_one(diagnosis, monkeypatch):
    # Decisions think MINIMAL by default (fast enum choices); GEMINI_PROFILE is for readings.
    transport = diagnosis(REPAIR, REPAIR, REPAIR)
    monkeypatch.setenv("GEMINI_PROFILE", "json")
    monkeypatch.delenv("DECISION_PROFILE", raising=False)
    assert GeminiDiagnostic.model_validate_json(_diagnose().text).profile == "minimal"
    monkeypatch.setenv("DECISION_PROFILE", "json")
    assert GeminiDiagnostic.model_validate_json(_diagnose().text).profile == "json"
    assert GeminiDiagnostic.model_validate_json(_diagnose(profile="default").text).profile == "default"
    assert transport.profiles == ["minimal", "json", "default"]


def test_the_agent_decides_with_the_decision_profile_and_reads_with_the_reading_one(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", service.ALLOWED_PROJECT)
    monkeypatch.delenv("DECISION_PROFILE", raising=False)
    monkeypatch.delenv("GEMINI_PROFILE", raising=False)
    assert service._make_policy().transport.profile.thinking_level == "MINIMAL"
    assert service._make_report_reader().transport.profile.thinking_level == "LOW"
    assert service._make_care_reader().transport.profile.thinking_level == "LOW"


def test_an_unknown_profile_is_refused(diagnosis, monkeypatch):
    transport = diagnosis(REPAIR)
    assert _diagnose(profile="fast").status_code == 422
    monkeypatch.setenv("DECISION_PROFILE", "fast")
    assert _diagnose().status_code == 503
    assert client.get("/health").json()["config"] == "misconfigured"
    monkeypatch.delenv("DECISION_PROFILE")
    monkeypatch.setenv("GEMINI_PROFILE", "fast")
    assert client.get("/health").json()["config"] == "misconfigured"
    assert transport.calls == []


def test_health_names_the_profile(diagnosis, monkeypatch):
    assert client.get("/health").json()["profile"] == "default"
    monkeypatch.setenv("GEMINI_PROFILE", "minimal")
    assert client.get("/health").json()["profile"] == "minimal"


def test_production_runs_with_the_configured_profile(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "physical-ai-guardian")
    monkeypatch.delenv("VERTEX_GEMINI_MODEL", raising=False)
    monkeypatch.setenv("GEMINI_PROFILE", "minimal")
    assert service._make_policy().transport.profile.name == "minimal"
    assert service._make_report_reader().transport.profile.name == "minimal"


def test_contract_profiles_match_the_transport():
    from typing import get_args

    from packages.agents.orchestrator import REQUEST_PROFILES
    from packages.domain.diagnostics import ProfileName

    assert set(get_args(ProfileName)) == set(REQUEST_PROFILES)
