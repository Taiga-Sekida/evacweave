"""Live Gemini on the public page, through the private agent service.

The public service holds no Vertex AI permission. It calls the private service with a
Google-signed ID token of its own service account (Cloud Run IAM), for three things:

- read_live: Gemini reads one line of visitor text (a situation report) and names at
  most two roads of a reviewed synthetic town. The answer schema only allows those
  roads' ids, and the ids are checked again here.
- run_live: Gemini drives the agent loop in a reviewed town (with the visitor's roads
  closed). The returned decisions are replayed through the Simulation Core here, and
  the run is shown only if every observation, verified result and the outcome match.
- read_care: Gemini reads the fictional residents' notes of a reviewed town against its
  shelters' facilities and names the shelters each resident must avoid. The answer
  schema only allows those residents' and shelters' ids, and they are checked again here.

Each one admits (_admit: off or busy is refused), holds RUNNING, takes one request from
the day's count, sends one timed request (_call_private), then checks the answer or
refuses it, and logs.

Spend guards: off unless LIVE_AI_ENABLED=1; one request at a time; LIVE_AI_DAILY_RUNS
requests per UTC day, a run, a report reading and a care reading counting one each,
counted in Cloud Storage on Cloud Run so a new instance does not start again at 0
(live_quota.py); at most 1 Gemini call per reading and 6 per run, enforced by the
private service; deadlines below the Cloud Run request timeout. They bound spend; they
are not a billing cap, so Cloud Billing spend caps stay necessary
(infra/cloud-run/COST_GUARD.md).
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request

from fastapi import HTTPException
from pydantic import ValidationError

from apps.api.live_quota import BUCKET_NAME, DailyQuota, QuotaUnavailable, StoredDailyQuota
from apps.http import NO_REDIRECT_OPENER as _OPENER
from packages.agents.loop import replay_matches
from packages.agents.orchestrator import GEMINI_MODELS
from packages.domain.agent import AgentTrace
from packages.domain.ai import AgentRunRequest
from packages.domain.care import CareReading, CareReadRequest, LiveCareReading
from packages.domain.io import parse_contract
from packages.domain.live import LiveRun, LiveStatus
from packages.domain.models import Contract, Plan, Scenario
from packages.domain.report import LiveReading, ReportReading, ReportReadRequest, RoadLabel
from packages.services.structured_log import structured_logger, trace_field, well_formed_trace_header

ORCHESTRATOR_HOST_PREFIX = "disaster-agentic-orchestrator-"
METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity"
)
DEFAULT_DAILY_RUNS = 40
MAX_DAILY_RUNS = 200
# The private service answers within its Cloud Run timeout (60 s: its run halts by
# 52 s, its reading by 35 s); the rest covers its cold start. The public Cloud Run
# request timeout (90 s, scripts/deploy_cloud_run_mvp.sh) stays above both.
LIVE_RUN_TIMEOUT_S = 70.0
LIVE_READ_TIMEOUT_S = 45.0
LIVE_CARE_TIMEOUT_S = 66.0  # the care reading has a longer budget (56 s) than a report's
PUBLIC_CLOUD_RUN_TIMEOUT_S = 90
# The request's own socket timeout (one of the three above) normally fires first;
# asyncio.wait_for, this much later, is the backstop for a thread that does not return.
THREAD_GRACE_S = 3.0
MAX_RESPONSE_BYTES = 1_048_576

logger = structured_logger("evacweave.public.live")

# _post_run, _post_read and _post_care: (origin, body, trace header) -> the raw answer.
PostToPrivate = Callable[[str, bytes, str | None], bytes]


def orchestrator_origin(url: str) -> str:
    """The private service origin, or ValueError: the ID token goes nowhere else."""
    parts = urlsplit(url.strip().rstrip("/"))
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host.startswith(ORCHESTRATOR_HOST_PREFIX)
        or not host.endswith(".run.app")
        or parts.port is not None
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise ValueError("ORCHESTRATOR_URL must be the private Cloud Run service origin")
    return f"https://{host}"


@dataclass(frozen=True)
class LiveSettings:
    origin: str
    daily_runs: int
    quota_bucket: str | None = None  # None: counted in memory (local development only)


def live_settings() -> LiveSettings | None:
    """None while live AI is off or misconfigured (logged), so nothing is called."""
    if os.environ.get("LIVE_AI_ENABLED", "0") != "1":
        return None
    bucket = os.environ.get("LIVE_AI_QUOTA_BUCKET", "").strip() or None
    try:
        origin = orchestrator_origin(os.environ.get("ORCHESTRATOR_URL", ""))
        daily = int(os.environ.get("LIVE_AI_DAILY_RUNS", str(DEFAULT_DAILY_RUNS)))
        if bucket is not None and not BUCKET_NAME.fullmatch(bucket):
            raise ValueError("LIVE_AI_QUOTA_BUCKET is not a bucket name")
    except ValueError:
        logger.warning("live_ai_misconfigured")
        return None
    if bucket is None and os.environ.get("K_SERVICE"):
        # On Cloud Run a count in memory starts again at 0 on every new instance.
        logger.warning("live_ai_misconfigured", extra={"fields": {"reason": "no_quota_bucket"}})
        return None
    return LiveSettings(origin=origin, daily_runs=max(1, min(daily, MAX_DAILY_RUNS)), quota_bucket=bucket)


def _today() -> date:
    return datetime.now(UTC).date()


QUOTA = DailyQuota()
# Never awaited while held elsewhere: a busy run is refused, not queued.
RUNNING = asyncio.Lock()


def _quota(settings: LiveSettings) -> DailyQuota | StoredDailyQuota:
    return StoredDailyQuota(settings.quota_bucket) if settings.quota_bucket else QUOTA


def status() -> LiveStatus:
    settings = live_settings()
    limit = settings.daily_runs if settings else 0
    left = 0
    if settings is not None:
        try:
            left = _quota(settings).left(limit, _today())
        except QuotaUnavailable as exc:  # shown as none left: the same answer a run would get
            logger.warning("live_ai_quota_unavailable", extra={"fields": {"error": str(exc)}})
    return LiveStatus(
        enabled=settings is not None,
        busy=RUNNING.locked(),
        daily_limit=limit,
        runs_left_today=left,
    )


def _identity_token(audience: str) -> str:
    """A Google-signed ID token for audience, from the Cloud Run metadata server."""
    request = Request(  # noqa: S310 - fixed metadata-server URL
        METADATA_IDENTITY_URL + "?" + urlencode({"audience": audience}),
        headers={"Metadata-Flavor": "Google"},
    )
    with _OPENER.open(request, timeout=5) as response:
        raw: bytes = response.read(8192)
    token = raw.decode("ascii", errors="replace").strip()
    if token.count(".") != 2:
        raise ValueError("metadata server returned no identity token")
    return token


def _post(origin: str, path: str, body: bytes, trace_header: str | None, timeout: float) -> bytes:
    headers = {"Authorization": f"Bearer {_identity_token(origin)}", "Content-Type": "application/json"}
    if trace_header:
        headers["X-Cloud-Trace-Context"] = trace_header
    request = Request(  # noqa: S310 - origin validated by orchestrator_origin()
        origin + path, data=body, method="POST", headers=headers
    )
    with _OPENER.open(request, timeout=timeout) as response:  # 3xx raises, never followed
        raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return raw


def _post_run(origin: str, body: bytes, trace_header: str | None) -> bytes:
    return _post(origin, "/v1/agent/run", body, trace_header, LIVE_RUN_TIMEOUT_S)


def _post_read(origin: str, body: bytes, trace_header: str | None) -> bytes:
    return _post(origin, "/v1/report/read", body, trace_header, LIVE_READ_TIMEOUT_S)


def _post_care(origin: str, body: bytes, trace_header: str | None) -> bytes:
    return _post(origin, "/v1/care/read", body, trace_header, LIVE_CARE_TIMEOUT_S)


def _refuse(status_code: int, detail: str, reason: str, fields: dict[str, Any]) -> HTTPException:
    logger.warning("live_ai_refused", extra={"fields": {**fields, "reason": reason}})
    return HTTPException(status_code=status_code, detail=detail)


def _trace_context(trace_header: str | None) -> tuple[str | None, dict[str, Any]]:
    """Forward only a well-formed trace header, so both services' logs share the request trace."""
    forwarded = trace_header if well_formed_trace_header(trace_header) else None
    return forwarded, trace_field(os.environ.get("GOOGLE_CLOUD_PROJECT", ""), forwarded)


def _admit(settings: LiveSettings | None, fields: dict[str, Any]) -> LiveSettings:
    if settings is None:
        raise _refuse(503, "live AI is disabled", "disabled", fields)
    if RUNNING.locked():
        raise _refuse(429, "another live run is in progress", "busy", fields)
    return settings


async def _take_one_from_daily_quota(settings: LiveSettings, fields: dict[str, Any]) -> int:
    """Take one request from today's count before anything is billed; the requests left."""
    try:
        left = await asyncio.to_thread(_quota(settings).take, settings.daily_runs, _today())
    except QuotaUnavailable as exc:
        fields = {**fields, "error": str(exc)}
        raise _refuse(503, "live run limit unavailable", "quota_unavailable", fields) from exc
    if left is None:
        raise _refuse(429, "daily live run limit reached", "daily_limit", fields)
    return left


async def _call_private(
    post: PostToPrivate,
    settings: LiveSettings,
    body: bytes,
    forwarded: str | None,
    timeout: float,
    fields: dict[str, Any],
) -> tuple[bytes, int]:
    """One request to the private service and the time it took (ms); its failures become
    gateway errors."""
    started = time.monotonic()
    try:
        raw: bytes = await asyncio.wait_for(
            asyncio.to_thread(post, settings.origin, body, forwarded), timeout=timeout + THREAD_GRACE_S
        )
    except HTTPError as exc:
        code = 504 if exc.code == 504 else 502
        raise _refuse(code, "agent service failed", f"agent_http_{exc.code}", fields) from exc
    except (OSError, ValueError) as exc:
        # OSError covers URLError, socket timeouts and a missing metadata server.
        reason = getattr(exc, "reason", None)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        raise _refuse(
            504 if timed_out else 502,
            "agent service timeout" if timed_out else "agent service unavailable",
            type(exc).__name__,
            fields,
        ) from exc
    return raw, int((time.monotonic() - started) * 1000)


def _parse_or_refuse[C: Contract](
    raw: bytes, contract: type[C], detail: str, reason: str, fields: dict[str, Any]
) -> C:
    """The private service's answer as a contract, or a 502 refusal with detail and reason."""
    try:
        return parse_contract(raw, contract)
    except (UnicodeError, ValueError, ValidationError) as exc:
        raise _refuse(502, detail, reason, fields) from exc


async def run_live(
    key: str,
    scenario: Scenario,
    plan: Plan,
    trace_header: str | None,
    closed_roads: tuple[str, ...] = (),
) -> LiveRun:
    """One Gemini run of a fixed scenario, returned only after the Core reproduces it."""
    forwarded, trace = _trace_context(trace_header)
    fields: dict[str, Any] = {"scenario": key, "closed_roads": list(closed_roads), **trace}
    settings = _admit(live_settings(), fields)
    async with RUNNING:
        runs_left = await _take_one_from_daily_quota(settings, fields)
        body = AgentRunRequest(scenario=scenario, plan=plan).model_dump_json().encode("utf-8")
        raw, duration_ms = await _call_private(
            _post_run, settings, body, forwarded, LIVE_RUN_TIMEOUT_S, fields
        )
        result = _parse_or_refuse(
            raw, AgentTrace, "agent answer is not a valid trace", "invalid_trace", fields
        )
        fields.update(run_id=result.run_id, model=result.model)
        if result.policy != "vertex-gemini" or result.model not in GEMINI_MODELS or result.run_id is None:
            raise _refuse(502, "agent answer is not a Gemini run", "not_gemini", fields)
        # Recompute every observation, tool result and the outcome from the decisions alone.
        if not await replay_matches(scenario, plan, result):
            raise _refuse(502, "agent answer failed Core re-verification", "replay_mismatch", fields)
        logger.info(
            "live_ai_run",
            extra={
                "fields": {
                    **fields,
                    "outcome": result.outcome,
                    "model_calls": result.model_calls,
                    "steps": len(result.steps),
                    "duration_ms": duration_ms,
                    "runs_left_today": runs_left,
                }
            },
        )
        return LiveRun(
            scenario=key,
            trace=result,
            duration_ms=duration_ms,
            runs_left_today=runs_left,
            closed_roads=closed_roads,
        )


async def read_live(
    key: str,
    roads: tuple[RoadLabel, ...],
    report: str,
    trace_header: str | None,
) -> LiveReading:
    """Gemini names the roads a visitor's report says are impassable, from roads only."""
    forwarded, trace = _trace_context(trace_header)
    # The report is visitor text: only its length is logged.
    fields: dict[str, Any] = {"scenario": key, "report_chars": len(report), **trace}
    settings = _admit(live_settings(), fields)
    async with RUNNING:
        runs_left = await _take_one_from_daily_quota(settings, fields)
        body = ReportReadRequest(report=report, roads=roads).model_dump_json().encode("utf-8")
        raw, duration_ms = await _call_private(
            _post_read, settings, body, forwarded, LIVE_READ_TIMEOUT_S, fields
        )
        reading = _parse_or_refuse(
            raw, ReportReading, "reader answer is not a valid reading", "invalid_reading", fields
        )
        fields.update(run_id=reading.run_id, model=reading.model)
        if reading.model not in GEMINI_MODELS:
            raise _refuse(502, "reader answer is not from Gemini", "not_gemini", fields)
        if not set(reading.closed_roads) <= {road.id for road in roads}:
            raise _refuse(502, "reader named a road it was not offered", "unknown_road", fields)
        logger.info(
            "live_ai_read",
            extra={
                "fields": {
                    **fields,
                    "closed_roads": list(reading.closed_roads),
                    "duration_ms": duration_ms,
                    "runs_left_today": runs_left,
                }
            },
        )
        return LiveReading(scenario=key, reading=reading, duration_ms=duration_ms, runs_left_today=runs_left)


async def read_care(key: str, request: CareReadRequest, trace_header: str | None) -> LiveCareReading:
    """Gemini names the shelters each resident's notes rule out, from residents and shelters only."""
    forwarded, trace = _trace_context(trace_header)
    # Notes describe (fictional) people: only how many there were is logged.
    fields: dict[str, Any] = {"scenario": key, "residents": len(request.residents), **trace}
    settings = _admit(live_settings(), fields)
    async with RUNNING:
        runs_left = await _take_one_from_daily_quota(settings, fields)
        body = request.model_dump_json().encode("utf-8")
        raw, duration_ms = await _call_private(
            _post_care, settings, body, forwarded, LIVE_CARE_TIMEOUT_S, fields
        )
        reading = _parse_or_refuse(
            raw, CareReading, "reader answer is not a valid reading", "invalid_reading", fields
        )
        fields.update(run_id=reading.run_id, model=reading.model)
        if reading.model not in GEMINI_MODELS:
            raise _refuse(502, "reader answer is not from Gemini", "not_gemini", fields)
        actors = {resident.actor_id for resident in request.residents}
        shelters = {shelter.id for shelter in request.shelters}
        if any(r.actor_id not in actors or r.shelter_id not in shelters for r in reading.restrictions):
            raise _refuse(
                502, "reader named a resident or shelter it was not offered", "unknown_target", fields
            )
        logger.info(
            "live_ai_care",
            extra={
                "fields": {
                    **fields,
                    "restrictions": len(reading.restrictions),
                    "duration_ms": duration_ms,
                    "runs_left_today": runs_left,
                }
            },
        )
        return LiveCareReading(
            scenario=key, reading=reading, duration_ms=duration_ms, runs_left_today=runs_left
        )
