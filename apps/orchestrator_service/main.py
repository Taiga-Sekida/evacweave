"""Private Cloud Run service: the guarded agent loop with Gemini as its policy.

Security boundary:
- Cloud Run IAM is the caller-authentication layer; this app is not intended for
  unauthenticated public access.
- The request contains Scenario + Plan. Every SimulationResult is computed here, so a
  caller cannot inject a fake PASS/FAIL into the model prompt.
- Gemini only picks one decision per step from the actions the loop allows. Anything
  else halts the run. At most LIMITS.max_model_calls Gemini attempts per run.
- /v1/report/read: Gemini reads one line of visitor text and names at most two roads,
  each from the roads in the request (an enum in the answer schema). The text is
  logged only by its length.
- /v1/care/read: Gemini reads residents' notes against the shelters' facilities and names
  the shelters each must avoid, with a reason from a fixed list; ids and reasons are
  enums in the answer schema. Notes are logged only by count.
- /v1/diagnostics/gemini: at most one Gemini request, on an explicit paid-call
  confirmation, three per day; reports where a live run fails.
- Every step is logged as structured JSON for Cloud Logging, correlated to the request.
  A Gemini request is logged with its purpose, model, time taken and, when it fails,
  only the exception class and HTTP status: never the prompt, a report, the answer
  or a credential.

Time budget, counted from the moment a request arrives. The Gemini client (SDK import,
credentials) is prepared first, outside any call's timer: on a cold instance it takes
seconds. One Gemini request may take its call timeout; the SDK abandons it at the same
limit and makes one attempt only (a retry would be an unreserved billable call). No
request is sent, or waited for, after the model deadline: each call is cut to what is
left, and a run out of time halts with a trace (fail closed). The outer timeout is the
backstop for everything else (504). Per endpoint, call <= model deadline < timeout:
- run:        MODEL_CALL_TIMEOUT_S <= RUN_MODEL_DEADLINE_S < RUN_TIMEOUT_S
- report:     MODEL_CALL_TIMEOUT_S <= READ_MODEL_DEADLINE_S < READ_TIMEOUT_S
- care:       CARE_CALL_TIMEOUT_S <= CARE_MODEL_DEADLINE_S < CARE_TIMEOUT_S
- diagnostic: DIAGNOSTIC_CALL_TIMEOUT_S <= DIAGNOSTIC_DEADLINE_S (no outer timeout)
All stay under CLOUD_RUN_TIMEOUT_S, the --timeout in
scripts/deploy_cloud_run_orchestrator.sh, so the answer comes back rather than a
gateway error. tests/test_gemini_calls.py (test_timeouts_nest_from_one_call_to_the_public_request)
checks this nesting against the deploy scripts and the public service's waits.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import time
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from functools import lru_cache
from typing import Any, Literal, Protocol, cast

from fastapi import FastAPI, HTTPException, Request

from apps.http import read_contract
from packages.agents.care_reader import GeminiCareReader
from packages.agents.loop import PolicyOutputError, run_agent
from packages.agents.orchestrator import (
    DEFAULT_GEMINI_MODEL,
    DEFAULT_PROFILE,
    GEMINI_MODELS,
    REQUEST_PROFILES,
    CallObserver,
    GeminiAgentPolicy,
    GoogleGenAITransport,
    ModelCall,
    api_status_code,
)
from packages.agents.report_reader import GeminiReportReader
from packages.domain.agent import AgentLimits, AgentObservation, AgentStep, AgentTrace
from packages.domain.ai import AgentRunRequest
from packages.domain.care import CareReading, CareReadRequest
from packages.domain.diagnostics import GeminiDiagnostic, GeminiDiagnosticRequest, ProfileName
from packages.domain.report import ReportReading, ReportReadRequest
from packages.services.budget import AttemptBudget
from packages.services.structured_log import structured_logger, trace_field

logger = structured_logger("evacweave.agent")

# --- request bodies ------------------------------------------------------------------
MAX_REQUEST_BYTES = 131_072
MAX_READING_REQUEST_BYTES = 16_384  # a report reading or a care reading
MAX_DIAGNOSTIC_REQUEST_BYTES = 1024

# --- Vertex AI configuration (checked on every request by _settings and _profile) ----
DEFAULT_MODEL = DEFAULT_GEMINI_MODEL
ALLOWED_MODELS = GEMINI_MODELS
ALLOWED_PROJECT = "physical-ai-guardian"
ALLOWED_LOCATION = "global"
# Vertex AI endpoints a deployment may switch to (GOOGLE_CLOUD_LOCATION) when the shared
# capacity of one is exhausted (HTTP 429). The data is synthetic.
ALLOWED_LOCATIONS = frozenset({ALLOWED_LOCATION, "us-central1", "asia-northeast1"})
# A decision picks one enum pair from a short menu: it needs little thinking, and every
# step waits for it. With thinking LOW, decisions that took 2 s took over 22 s on
# 2026-10-14 and hit the call limit (Vertex 504), so decisions think MINIMAL unless
# DECISION_PROFILE says otherwise. Readings keep GEMINI_PROFILE (thinking LOW).
DEFAULT_DECISION_PROFILE = "minimal"

# --- output tokens -------------------------------------------------------------------
# Output is a ~30 token JSON object; the headroom keeps thinking tokens from truncating it.
MAX_OUTPUT_TOKENS = 512
READER_MAX_OUTPUT_TOKENS = 1024  # reading free text against a town's roads thinks longer
CARE_MAX_OUTPUT_TOKENS = 8192  # one answer covers every resident; thinking shares the limit

# --- time budget, in seconds (see the module docstring) ------------------------------
CLOUD_RUN_TIMEOUT_S = 60
MODEL_CALL_TIMEOUT_S = 25.0
RUN_MODEL_DEADLINE_S = 45.0
RUN_TIMEOUT_S = 52.0
READ_MODEL_DEADLINE_S = 30.0
READ_TIMEOUT_S = 35.0
# A care reading answers for every resident at once: it took 14 to 18 s in production
# and once over 36 s (2026-10-14), so it has its own budget, within Cloud Run's 60 s.
CARE_CALL_TIMEOUT_S = 50.0
CARE_MODEL_DEADLINE_S = 52.0
CARE_TIMEOUT_S = 56.0
# A diagnostic waits longer than production does, to tell a slow answer from a hang.
DIAGNOSTIC_CALL_TIMEOUT_S = 45.0
DIAGNOSTIC_DEADLINE_S = 52.0

# --- call limits ---------------------------------------------------------------------
LIMITS = AgentLimits(max_steps=6, max_model_calls=6)
DIAGNOSTIC_DAILY_LIMIT = 3
# Runs and readings in progress at once (a diagnostic has its own lock): 2, as
# --concurrency 2 on the single instance (--max 1) in scripts/deploy_cloud_run_orchestrator.sh,
# held here as well so the bound does not rest on deploy flags alone. Waiting for a slot
# spends the request's own deadline.
AI_CONCURRENCY = asyncio.Semaphore(2)

app = FastAPI(
    title="EvacWeave Agent",
    version="0.5.0",
    description="Private, IAM-protected agent loop with Vertex Gemini as its policy.",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _settings() -> tuple[str, str, str]:
    project = os.environ.get("GOOGLE_CLOUD_PROJECT", "").strip()
    location = os.environ.get("GOOGLE_CLOUD_LOCATION", ALLOWED_LOCATION).strip()
    model = os.environ.get("VERTEX_GEMINI_MODEL", DEFAULT_MODEL).strip()

    if project != ALLOWED_PROJECT:
        raise RuntimeError("unexpected GOOGLE_CLOUD_PROJECT")
    if location not in ALLOWED_LOCATIONS:
        raise RuntimeError("unexpected GOOGLE_CLOUD_LOCATION")
    if model not in ALLOWED_MODELS:
        raise RuntimeError("VERTEX_GEMINI_MODEL is not allowlisted")
    return project, location, model


def _profile(variable: str, fallback: str) -> ProfileName:
    """The request profile named by an environment variable (REQUEST_PROFILES in
    packages/agents/orchestrator.py); RuntimeError for an unknown name."""
    name = os.environ.get(variable, fallback).strip()
    if name not in REQUEST_PROFILES:
        raise RuntimeError(f"{variable} is not a known request profile")
    return cast(ProfileName, name)


def _decision_profile() -> ProfileName:
    """The profile of the agent's decisions (DECISION_PROFILE)."""
    return _profile("DECISION_PROFILE", DEFAULT_DECISION_PROFILE)


def _reading_profile() -> ProfileName:
    """The profile of report and care readings (GEMINI_PROFILE)."""
    return _profile("GEMINI_PROFILE", DEFAULT_PROFILE)


@lru_cache(maxsize=4)
def _transport(project: str, location: str, profile: str) -> GoogleGenAITransport:
    return GoogleGenAITransport(project=project, location=location, profile=REQUEST_PROFILES[profile])


def _make_policy() -> GeminiAgentPolicy:
    project, location, model = _settings()
    return GeminiAgentPolicy(
        model=model,
        transport=_transport(project, location, _decision_profile()),
        budget=AttemptBudget(gemma_limit=0, gemini_limit=LIMITS.max_model_calls),
        max_output_tokens=MAX_OUTPUT_TOKENS,
        timeout_s=MODEL_CALL_TIMEOUT_S,
    )


def _make_report_reader() -> GeminiReportReader:
    project, location, model = _settings()
    return GeminiReportReader(
        model=model,
        transport=_transport(project, location, _reading_profile()),
        budget=AttemptBudget(gemma_limit=0, gemini_limit=1),
        max_output_tokens=READER_MAX_OUTPUT_TOKENS,
        timeout_s=MODEL_CALL_TIMEOUT_S,
    )


def _make_care_reader() -> GeminiCareReader:
    project, location, model = _settings()
    return GeminiCareReader(
        model=model,
        transport=_transport(project, location, _reading_profile()),
        budget=AttemptBudget(gemma_limit=0, gemini_limit=1),
        max_output_tokens=CARE_MAX_OUTPUT_TOKENS,
        timeout_s=CARE_CALL_TIMEOUT_S,
    )


def _build_or_503[T](factory: Callable[[], T]) -> T:
    """factory(), or a 503 when the service's own configuration is invalid."""
    try:
        return factory()
    except RuntimeError as exc:
        logger.error("agent_misconfigured")
        raise HTTPException(status_code=503, detail="agent unavailable") from exc


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def _request_context(run_id: str, model: str, request: Request, **fields: Any) -> dict[str, Any]:
    """The fields on every log entry of one request, ending with its Cloud Trace link."""
    return {
        "run_id": run_id,
        "model": model,
        **fields,
        **trace_field(ALLOWED_PROJECT, request.headers.get("x-cloud-trace-context")),
    }


def _call_logger(run_id: str) -> CallObserver:
    """Logs each Gemini request: purpose, model, time and, on failure, class and status."""

    def log(call: ModelCall) -> None:
        fields: dict[str, Any] = {
            "run_id": run_id,
            "model": call.model,
            "purpose": call.purpose,
            "elapsed_ms": call.elapsed_ms,
        }
        if call.error_type is None:
            logger.info("gemini_call", extra={"fields": fields})
        else:
            fields.update(error_type=call.error_type, status_code=call.status_code)
            if call.error_hint is not None:
                fields["error_hint"] = call.error_hint
            logger.warning("gemini_call_failed", extra={"fields": fields})

    return log


class ClientUnavailable(RuntimeError):
    """The Gemini client could not be prepared (credentials, SDK); no request was sent."""


async def _prepare(transport: object, run_id: str, model: str) -> int:
    """Prepare the transport's client outside any call's timer; returns the time it took."""
    prepare = getattr(transport, "prepare", None)
    started = time.monotonic()
    if prepare is None:  # test doubles have nothing to prepare
        return 0
    try:
        await prepare()
    except Exception as exc:
        logger.warning(
            "gemini_client_failed",
            extra={
                "fields": {
                    "run_id": run_id,
                    "model": model,
                    "error_type": type(exc).__name__,
                    "status_code": api_status_code(exc),
                    "elapsed_ms": _ms_since(started),
                }
            },
        )
        raise ClientUnavailable("Gemini client could not be prepared") from exc
    return _ms_since(started)


@app.get("/health")
def health() -> dict[str, str]:
    profile: str
    decision_profile: str
    try:
        _, location, model = _settings()
        profile = _reading_profile()
        decision_profile = _decision_profile()
        config = "ready"
    except RuntimeError:
        location = "invalid"
        model = "invalid"
        profile = "invalid"
        decision_profile = "invalid"
        config = "misconfigured"
    return {
        "status": "ok",
        "service": "disaster-agentic-orchestrator",
        "config": config,
        "vertex_location": location,
        "model": model,
        "profile": profile,
        "decision_profile": decision_profile,
        "revision": os.environ.get("APP_REVISION", "dev"),
    }


@app.post("/v1/agent/run", response_model=AgentTrace)
async def run(request: Request) -> AgentTrace:
    started = time.monotonic()
    payload = await read_contract(request, AgentRunRequest, max_bytes=MAX_REQUEST_BYTES)
    policy = _build_or_503(_make_policy)

    run_id = uuid.uuid4().hex
    policy.deadline = started + RUN_MODEL_DEADLINE_S
    policy.on_call = _call_logger(run_id)
    profile = getattr(getattr(policy.transport, "profile", None), "name", None)
    context = _request_context(run_id, policy.model, request, profile=profile)

    def log_step(step: AgentStep) -> None:
        logger.info(
            "agent_step",
            extra={
                "fields": {
                    **context,
                    "step": step.index,
                    "plan_status": step.observation.plan_status,
                    "allowed_actions": list(step.observation.allowed_actions),
                    "action": step.decision.action if step.decision else None,
                    "reason_code": step.decision.reason_code if step.decision else None,
                    "policy_error": step.policy_error,
                    "effect": step.effect.status if step.effect else None,
                    "model_calls": policy.model_calls,
                }
            },
        )

    async def bounded_run() -> AgentTrace:
        # Waiting for a slot counts against the same deadline as the run itself.
        async with AI_CONCURRENCY:
            await _prepare(policy.transport, run_id, policy.model)
            return await run_agent(
                payload.scenario, payload.plan, policy, limits=LIMITS, on_step=log_step, run_id=run_id
            )

    try:
        trace = await asyncio.wait_for(bounded_run(), timeout=RUN_TIMEOUT_S)
    except ClientUnavailable as exc:
        raise HTTPException(status_code=503, detail="agent unavailable") from exc
    except TimeoutError as exc:
        logger.warning("agent_timeout", extra={"fields": context})
        raise HTTPException(status_code=504, detail="agent timeout") from exc
    except Exception as exc:
        # Log the type only: a propagated traceback would echo the caller's input.
        logger.exception("agent_failed", extra={"fields": context})
        raise HTTPException(status_code=500, detail="agent failed") from exc

    logger.info(
        "agent_run",
        extra={
            "fields": {
                **context,
                "outcome": trace.outcome,
                "steps": len(trace.steps),
                "model_calls": trace.model_calls,
                "duration_ms": _ms_since(started),
            }
        },
    )
    return trace


# --- readings: one Gemini call each --------------------------------------------------


class _Reader[RequestT, ResultT](Protocol):
    """GeminiReportReader and GeminiCareReader, as _bounded_read uses them."""

    @property
    def model(self) -> str: ...

    @property
    def transport(self) -> object: ...

    async def read(self, request: RequestT, /) -> ResultT: ...


async def _bounded_read[RequestT, ResultT](
    reader: _Reader[RequestT, ResultT],
    payload: RequestT,
    *,
    run_id: str,
    timeout_s: float,
    event: Literal["report_read", "care_read"],
    context: dict[str, Any],
) -> ResultT:
    """One reading under the shared concurrency limit and timeout_s; failures become HTTP errors.

    Failures are logged as {event}_timeout, {event}_invalid_output or {event}_failed
    (report_read_timeout, care_read_failed, ...), with context only.
    """

    async def bounded() -> ResultT:
        # Waiting for a slot counts against the same timeout as the reading itself.
        async with AI_CONCURRENCY:
            await _prepare(reader.transport, run_id, reader.model)
            return await reader.read(payload)

    try:
        return await asyncio.wait_for(bounded(), timeout=timeout_s)
    except ClientUnavailable as exc:
        raise HTTPException(status_code=503, detail="agent unavailable") from exc
    except TimeoutError as exc:
        logger.warning(f"{event}_timeout", extra={"fields": context})
        raise HTTPException(status_code=504, detail="reader timeout") from exc
    except PolicyOutputError as exc:
        logger.warning(f"{event}_invalid_output", extra={"fields": context})
        raise HTTPException(status_code=502, detail="reader returned no valid reading") from exc
    except Exception as exc:
        logger.exception(f"{event}_failed", extra={"fields": context})
        raise HTTPException(status_code=500, detail="reader failed") from exc


@app.post("/v1/report/read", response_model=ReportReading)
async def read_report(request: Request) -> ReportReading:
    """Gemini names the roads a short report says are impassable (one model call)."""
    started = time.monotonic()
    payload = await read_contract(request, ReportReadRequest, max_bytes=MAX_READING_REQUEST_BYTES)
    reader = _build_or_503(_make_report_reader)

    run_id = uuid.uuid4().hex
    reader.deadline = started + READ_MODEL_DEADLINE_S
    reader.on_call = _call_logger(run_id)
    # The report is visitor text: only its length is logged.
    context = _request_context(
        run_id, reader.model, request, report_chars=len(payload.report), roads_offered=len(payload.roads)
    )
    roads = await _bounded_read(
        reader, payload, run_id=run_id, timeout_s=READ_TIMEOUT_S, event="report_read", context=context
    )

    logger.info(
        "report_read",
        extra={
            "fields": {
                **context,
                "closed_roads": list(roads),
                "model_calls": reader.model_calls,
                "duration_ms": _ms_since(started),
            }
        },
    )
    return ReportReading(run_id=run_id, model=reader.model, closed_roads=roads)


@app.post("/v1/care/read", response_model=CareReading)
async def read_care(request: Request) -> CareReading:
    """Gemini names, per resident, the shelters their circumstances rule out (one model call)."""
    started = time.monotonic()
    payload = await read_contract(request, CareReadRequest, max_bytes=MAX_READING_REQUEST_BYTES)
    reader = _build_or_503(_make_care_reader)

    run_id = uuid.uuid4().hex
    reader.deadline = started + CARE_MODEL_DEADLINE_S
    reader.on_call = _call_logger(run_id)
    # Notes describe (fictional) people: only how many there were is logged.
    context = _request_context(
        run_id, reader.model, request, residents=len(payload.residents), shelters=len(payload.shelters)
    )
    restrictions = await _bounded_read(
        reader, payload, run_id=run_id, timeout_s=CARE_TIMEOUT_S, event="care_read", context=context
    )

    logger.info(
        "care_read",
        extra={
            "fields": {
                **context,
                "restrictions": len(restrictions),
                "model_calls": reader.model_calls,
                "duration_ms": _ms_since(started),
            }
        },
    )
    return CareReading(run_id=run_id, model=reader.model, restrictions=restrictions)


# --- one-call diagnostic -------------------------------------------------------------

# The agent's first turn on the bundled base scenario: a failing plan, repair or escalate.
DIAGNOSTIC_OBSERVATION = AgentObservation.model_validate_json(
    json.dumps(
        {
            "step": 0,
            "steps_remaining": LIMITS.max_steps,
            "plan_id": "diagnostic_plan",
            "plan_status": "FAIL",
            "residents": 2,
            "evacuated": 0,
            "rejection_reasons": ["route_blocked"],
            "repair_attempted": False,
            "stress_tested": False,
            "single_points_of_failure": 0,
            "contingencies_planned": False,
            "contingencies_covered": 0,
            "allowed_actions": ["run_repair", "escalate"],
        }
    )
)
DIAGNOSTIC_REPORT = ReportReadRequest.model_validate_json(
    json.dumps(
        {
            "report": "橋が冠水して渡れないと消防から連絡",
            "roads": [
                {"id": "river_path", "name": "川沿いから橋への道", "between": ["川沿い", "橋のたもと"]},
                {"id": "river_bridge", "name": "橋", "between": ["橋のたもと", "中央"]},
                {"id": "school_access", "name": "小学校への道", "between": ["中央", "小学校"]},
            ],
        },
        ensure_ascii=False,
    )
)
FailedStage = Literal["prepare", "call", "answer"]


def _today() -> date:
    return datetime.now(UTC).date()


class DailyCount:
    """Diagnostics started per UTC day on this instance (the service runs at most one)."""

    def __init__(self) -> None:
        self._day: date | None = None
        self._used = 0

    def left(self, limit: int) -> int:
        if self._day != _today():
            self._day, self._used = _today(), 0
        return max(0, limit - self._used)

    def take(self, limit: int) -> bool:
        if self.left(limit) == 0:
            return False
        self._used += 1
        return True


DIAGNOSTICS = DailyCount()
# Never awaited while held: a second diagnostic is refused, not queued.
DIAGNOSTIC_RUNNING = asyncio.Lock()


def _caller_email(authorization: str | None) -> str | None:
    """The email claim of the caller's ID token, which Cloud Run has already verified.

    Only read here, never trusted for anything Cloud Run IAM has not checked: it tells
    a person (an operator in Cloud Shell) from a service account.
    """
    scheme, _, token = (authorization or "").partition(" ")
    parts = token.split(".")
    if scheme.lower() != "bearer" or len(parts) < 2 or len(parts[1]) > 4096:
        return None
    try:
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (ValueError, UnicodeDecodeError):
        return None
    email = claims.get("email") if isinstance(claims, dict) else None
    return email if isinstance(email, str) else None


def _refuse_service_accounts(request: Request) -> None:
    """403 for a caller whose verified ID token belongs to a service account."""
    caller = _caller_email(request.headers.get("authorization"))
    if caller is not None and caller.endswith(".gserviceaccount.com"):
        raise HTTPException(status_code=403, detail="diagnostics are for operators")


def _diagnostic_settings(payload: GeminiDiagnosticRequest) -> tuple[str, str, str, ProfileName]:
    """(project, location, model, profile): the profile asked for, else production's for the purpose."""
    project, location, model = _settings()
    profile = payload.profile or (
        _decision_profile() if payload.purpose == "decision" else _reading_profile()
    )
    return project, location, model, profile


async def _send_one_diagnostic(
    payload: GeminiDiagnosticRequest,
    model: str,
    transport: GoogleGenAITransport,
    deadline: float,
    observe: CallObserver,
) -> None:
    """The one Gemini request, shaped like production's first decision or a report reading."""
    budget = AttemptBudget(gemma_limit=0, gemini_limit=1)
    if payload.purpose == "decision":
        await GeminiAgentPolicy(
            model=model,
            transport=transport,
            budget=budget,
            max_output_tokens=MAX_OUTPUT_TOKENS,
            timeout_s=DIAGNOSTIC_CALL_TIMEOUT_S,
            deadline=deadline,
            on_call=observe,
        ).decide(DIAGNOSTIC_OBSERVATION)
    else:
        await GeminiReportReader(
            model=model,
            transport=transport,
            budget=budget,
            max_output_tokens=READER_MAX_OUTPUT_TOKENS,
            timeout_s=DIAGNOSTIC_CALL_TIMEOUT_S,
            deadline=deadline,
            on_call=observe,
        ).read(DIAGNOSTIC_REPORT)


def _failed_stage(exc: Exception) -> FailedStage:
    # An answer that is not valid structured output (PolicyOutputError, JSON or
    # schema errors) is a ValueError; anything else failed the request itself.
    return "answer" if isinstance(exc, ValueError) else "call"


def _log_diagnostic(result: GeminiDiagnostic) -> None:
    logger.info(
        "gemini_diagnostic",
        extra={
            "fields": {
                "run_id": result.run_id,
                "model": result.model,
                "profile": result.profile,
                "purpose": result.purpose,
                "ok": result.ok,
                "failed_stage": result.failed_stage,
                "error_type": result.error_type,
                "status_code": result.status_code,
                "prepare_ms": result.prepare_ms,
                "call_ms": result.call_ms,
            }
        },
    )


@app.post("/v1/diagnostics/gemini", response_model=GeminiDiagnostic)
async def diagnose_gemini(request: Request) -> GeminiDiagnostic:
    """PAID: at most one Gemini request, the same shape as production's, to find why runs fail.

    For operators only: a service account (the public service's included) is refused,
    so a compromised public service cannot spend diagnostics.
    """
    started = time.monotonic()
    _refuse_service_accounts(request)
    payload = await read_contract(request, GeminiDiagnosticRequest, max_bytes=MAX_DIAGNOSTIC_REQUEST_BYTES)
    project, location, model, profile = _build_or_503(lambda: _diagnostic_settings(payload))
    if DIAGNOSTIC_RUNNING.locked():
        raise HTTPException(status_code=429, detail="a diagnostic is already running")
    async with DIAGNOSTIC_RUNNING:
        if not DIAGNOSTICS.take(DIAGNOSTIC_DAILY_LIMIT):
            raise HTTPException(status_code=429, detail="daily diagnostic limit reached")
        run_id = uuid.uuid4().hex
        calls: list[ModelCall] = []
        log_call = _call_logger(run_id)

        def observe(call: ModelCall) -> None:
            calls.append(call)
            log_call(call)

        transport = _transport(project, location, profile)
        failed_stage: FailedStage | None = None
        error: BaseException | None = None
        try:
            prepare_ms = await _prepare(transport, run_id, model)
        except ClientUnavailable as exc:
            prepare_ms = _ms_since(started)
            failed_stage, error = "prepare", exc.__cause__ or exc
        if failed_stage is None:
            deadline = started + DIAGNOSTIC_DEADLINE_S
            try:
                await _send_one_diagnostic(payload, model, transport, deadline, observe)
            except Exception as exc:  # noqa: BLE001 - reported by class and status, never re-raised
                failed_stage, error = _failed_stage(exc), exc
        call = calls[-1] if calls else None
        result = GeminiDiagnostic(
            run_id=run_id,
            revision=os.environ.get("APP_REVISION", "dev")[:64] or "dev",
            model=model,
            location=location,
            profile=profile,
            purpose=payload.purpose,
            ok=failed_stage is None,
            failed_stage=failed_stage,
            error_type=None if error is None else type(error).__name__,
            status_code=None if error is None else api_status_code(error),
            prepare_ms=prepare_ms,
            call_ms=None if call is None else call.elapsed_ms,
            model_calls=len(calls),
            production_call_timeout_ms=int(MODEL_CALL_TIMEOUT_S * 1000),
            diagnostics_left_today=DIAGNOSTICS.left(DIAGNOSTIC_DAILY_LIMIT),
        )
    _log_diagnostic(result)
    return result
