"""Public Cloud Run service: reproducible synthetic evidence with bounded inputs.

The showcase is deterministic: the agent loop runs here with the rule policy, and a
recorded Gemini run is shown only after replaying its decisions reproduces every
recorded result exactly. This service holds no Vertex AI permission. When live AI is
switched on, it asks the private service (Cloud Run IAM) to have Gemini read a
visitor's situation report (/v1/agent/read), read the fictional residents' notes
against the shelters (POST /v1/agent/care) and run the agent loop with Gemini in a
fixed scenario (/v1/agent/live), and checks all three answers here (live_agent.py).
"""

from __future__ import annotations

import asyncio
import math
import os
import threading
from collections.abc import Awaitable, Callable
from functools import lru_cache
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from apps.api import live_agent
from apps.api.labels import road_labels
from apps.api.rate_limit import (
    API,
    EVERYONE,
    LIVE_AI,
    LIVE_AI_ALL,
    LIVE_AI_DAY,
    SlidingWindow,
    client_of,
)
from apps.http import SECURITY_HEADERS, read_contract
from packages.agents.loop import RulePolicy, replay_matches, run_agent
from packages.domain.agent import AgentAction, AgentTrace, RecordedAgentRun, ToolEffect
from packages.domain.care import (
    PATTERNS,
    CareReadRequest,
    CareSet,
    CareSweep,
    LiveCareReading,
    LiveCareRequest,
    ResidentPool,
    care_pattern,
    pattern_avoids,
)
from packages.domain.io import load_contract
from packages.domain.live import ChallengeRequest, ChallengeRun, LiveRun, LiveRunRequest, LiveStatus
from packages.domain.models import Contract, Plan, Scenario, ShelterRestriction, SimulationResult
from packages.domain.report import LiveReading, LiveReadRequest
from packages.domain.showcase import ScenarioRun, ShowcaseEvidence
from packages.redteam.probes import with_roads_closed
from packages.repair.sweep import sweep
from packages.services.structured_log import structured_logger
from packages.simulation.engine import simulate

ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = ROOT / "data" / "demo_area" / "blocked_route"
# Reviewed synthetic worlds besides the base one: (key, title, folder with
# scenario.json/plan.json, and care.json/residents.json for care notes). The showcase
# (slide 9, the play page), the challenge, live Gemini, the care notes and the sweep
# accept these keys and no others.
MORE_SCENARIOS = (("river_district", "川沿いの地区", ROOT / "data" / "demo_area" / "river_district"),)
RECORDED_RUN_PATH = ROOT / "data" / "evidence" / "recorded_gemini_run.json"
STATIC_DIR = ROOT / "apps" / "web" / "static"
MAX_PUBLIC_BODY = 65536
MAX_LIVE_BODY = 8192  # closed roads and up to 64 shelter restrictions
MAX_CARE_BODY = 8192  # up to 16 rewritten notes of 100 characters, UTF-8
MAX_READ_BODY = 1024  # a 120-character report sent as UTF-8 (3 bytes per kana/kanji), with room
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; style-src 'self'; "
    "img-src 'self' data:; connect-src 'self'; "
    "object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
# Requests that spend the day's shared Gemini runs (POST only; GET /v1/agent/live is a status).
LIVE_AI_PATHS = frozenset({"/v1/agent/live", "/v1/agent/read", "/v1/agent/care"})
LIMITER = SlidingWindow()
SWEEP_LOCK = threading.Lock()

logger = structured_logger("evacweave.public")

app = FastAPI(
    title="EvacWeave — 避難計画の検証",
    version="0.5.0",
    description="合成データを用いる決定論的避難計画の検証試作版。",
    # The stock /docs and /redoc pages load Swagger UI/ReDoc from a CDN with inline
    # scripts, which the CSP below blocks (blank page). /api-guide replaces them.
    docs_url=None,
    redoc_url=None,
)
app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


class SimulationRequest(Contract):
    scenario: Scenario
    plan: Plan


# --- middleware ----------------------------------------------------------------------


def _trusted_proxies() -> int:
    # Load balancers of ours in front of Cloud Run (0 when visitors reach run.app directly).
    value = os.environ.get("TRUSTED_PROXY_HOPS", "0")
    return int(value) if value.isascii() and value.isdigit() and int(value) <= 4 else 0


def _rate_limits_on() -> bool:
    # On Cloud Run always; elsewhere (tests, a laptop) only when asked for.
    return bool(os.environ.get("K_SERVICE")) or os.environ.get("RATE_LIMIT_ENABLED") == "1"


# Registered before secure_headers, so that middleware wraps this one and a 429 gets
# the same headers as any other answer.
@app.middleware("http")
async def rate_limit(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    path = request.url.path
    if path.startswith("/v1/") and _rate_limits_on():
        client = client_of(
            request.headers.get("x-forwarded-for"),
            request.client.host if request.client else None,
            _trusted_proxies(),
        )
        checks = [(client, API)]
        if request.method == "POST" and path in LIVE_AI_PATHS:
            checks += [(client, LIVE_AI), (client, LIVE_AI_DAY), (EVERYONE, LIVE_AI_ALL)]
        for who, limit in checks:
            wait = LIMITER.wait(who, limit)
            if wait:
                # The address is not logged, and the path is cut short: which limit was
                # hit is enough to see a flood, and long URLs cannot inflate the logs.
                logger.warning("rate_limited", extra={"fields": {"limit": limit.name, "path": path[:48]}})
                return JSONResponse(
                    {"detail": "too many requests"},
                    status_code=429,
                    headers={"Retry-After": str(math.ceil(wait))},
                )
    return await call_next(request)


@app.middleware("http")
async def secure_headers(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    response = await call_next(request)
    response.headers.update(SECURITY_HEADERS)
    response.headers["Content-Security-Policy"] = CONTENT_SECURITY_POLICY
    if request.url.path.startswith("/assets/") and response.status_code == 200:
        # Scripts and styles are public and the same for everyone: browsers may keep them
        # for a few minutes, so a crowd costs the single instance fewer requests.
        response.headers["Cache-Control"] = "public, max-age=300"
    return response


# --- reviewed scenarios --------------------------------------------------------------
# Each loader below is cached by scenario key and reached only through its guard, which
# refuses an unknown key first (_known). Otherwise a visitor could push the real entries
# out of a cache with made-up keys and make each request compute again.


def _fixture(plan_name: str = "plan.json") -> tuple[Scenario, Plan]:
    return (
        load_contract(DEMO_DIR / "scenario.json", Scenario),
        load_contract(DEMO_DIR / plan_name, Plan),
    )


def _folder_inputs(folder: Path) -> tuple[Scenario, Plan]:
    return load_contract(folder / "scenario.json", Scenario), load_contract(folder / "plan.json", Plan)


def _known(key: str) -> bool:
    """The base scenario or one of MORE_SCENARIOS."""
    return key == "base" or any(key == extra for extra, _, _ in MORE_SCENARIOS)


def _reviewed_inputs(key: str) -> tuple[Scenario, Plan] | None:
    """The reviewed synthetic scenarios a visitor may run, by key; nothing else."""
    return _load_reviewed_inputs(key) if _known(key) else None


@lru_cache(maxsize=4)
def _load_reviewed_inputs(key: str) -> tuple[Scenario, Plan] | None:
    if key == "base":
        return _fixture()
    for extra, _, folder in MORE_SCENARIOS:
        if key == extra:
            return _folder_inputs(folder)
    return None


def _care_files(key: str) -> tuple[CareSet, ResidentPool | None] | None:
    """A scenario's reviewed care notes and, if it has one, its pool of residents."""
    return _load_care_files(key) if _known(key) else None


@lru_cache(maxsize=4)
def _load_care_files(key: str) -> tuple[CareSet, ResidentPool | None] | None:
    for extra, _, folder in MORE_SCENARIOS:
        if key == extra and (folder / "care.json").is_file():
            pool = folder / "residents.json"
            return (
                load_contract(folder / "care.json", CareSet),
                load_contract(pool, ResidentPool) if pool.is_file() else None,
            )
    return None


def _care_set(key: str, pattern: int = 0) -> CareSet | None:
    """The reviewed care notes of one group of residents: fictional residents and shelters."""
    return _load_care_set(key, pattern) if _known(key) and 0 <= pattern < PATTERNS else None


@lru_cache(maxsize=64)
def _load_care_set(key: str, pattern: int) -> CareSet | None:
    files = _care_files(key)
    if files is None:
        return None
    care, pool = files
    if pool is None:
        return care if pattern == 0 else None
    return care_pattern(care, pool, pattern)


def _closed_world(
    key: str, closed: tuple[str, ...], restrictions: tuple[ShelterRestriction, ...] = ()
) -> tuple[Scenario, Plan]:
    """A reviewed scenario with some of its own roads closed for the whole window, and
    residents kept out of the shelters their circumstances rule out."""
    inputs = _reviewed_inputs(key)
    if inputs is None:
        raise HTTPException(status_code=422, detail="unknown scenario")
    scenario, plan = inputs
    world = with_roads_closed(scenario, closed)
    if world is None:
        raise HTTPException(status_code=422, detail="unknown or repeated road")
    if restrictions:
        try:
            world = Scenario.model_validate(world.model_copy(update={"restrictions": restrictions}))
        except ValueError as exc:  # pydantic's ValidationError: a resident or shelter of another town
            raise HTTPException(status_code=422, detail="unknown or repeated restriction") from exc
    return world, plan


def _sorted_restrictions(restrictions: tuple[ShelterRestriction, ...]) -> tuple[ShelterRestriction, ...]:
    return tuple(sorted(restrictions, key=lambda r: (r.actor_id, r.shelter_id, r.reason)))


# --- health, fixed simulations and the showcase --------------------------------------


def _effect(trace: AgentTrace, action: AgentAction) -> ToolEffect | None:
    for step in trace.steps:
        if step.decision and step.decision.action == action and step.effect:
            return step.effect
    return None


def _recorded_ai_run(
    scenario: Scenario, plan: Plan
) -> tuple[RecordedAgentRun | None, Literal["absent", "replay_verified", "rejected"]]:
    """Load a recorded Gemini run; show it only if replaying its decisions reproduces it."""
    if not RECORDED_RUN_PATH.is_file():
        return None, "absent"
    try:
        recorded = load_contract(RECORDED_RUN_PATH, RecordedAgentRun)
        verified = asyncio.run(replay_matches(scenario, plan, recorded.trace))
    except (OSError, UnicodeError, ValueError):
        verified = False
    if not verified:
        logger.warning("recorded_ai_run_rejected", extra={"fields": {"path": RECORDED_RUN_PATH.name}})
        return None, "rejected"
    return recorded, "replay_verified"


def _scenario_run(key: str, title: str, folder: Path) -> ScenarioRun:
    scenario, plan = _folder_inputs(folder)
    trace = asyncio.run(run_agent(scenario, plan, RulePolicy()))
    return ScenarioRun(key=key, title=title, scenario=scenario, baseline_plan=plan, agent_trace=trace)


@lru_cache(maxsize=1)
def _showcase() -> ShowcaseEvidence:
    """Every displayed figure comes from checked-in fixtures and actual Core runs."""
    scenario, baseline = _fixture()
    trace = asyncio.run(run_agent(scenario, baseline, RulePolicy()))
    # The demo needs a verified repair to show; the outcome itself may be a hand-over.
    if trace.final_plan is None or trace.final_result is None:
        raise RuntimeError("synthetic demo agent run produced no verified plan")
    stress = _effect(trace, "run_redteam")
    contingency = _effect(trace, "plan_contingency")
    recorded, status = _recorded_ai_run(scenario, baseline)
    return ShowcaseEvidence(
        scenario=scenario,
        baseline_plan=baseline,
        baseline_result=trace.initial_result,
        repaired_plan=trace.final_plan,
        repaired_result=trace.final_result,
        probes=stress.probes if stress else (),
        contingencies=contingency.contingencies if contingency else (),
        agent_trace=trace,
        more_runs=tuple(_scenario_run(*entry) for entry in MORE_SCENARIOS),
        recorded_ai_run=recorded,
        recorded_ai_run_status=status,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "service": "disaster-agentic-mvp",
        "revision": os.environ.get("APP_REVISION", "dev"),
    }


@app.post("/v1/simulate", response_model=SimulationResult)
async def simulate_request(request: Request) -> SimulationResult:
    # Anonymous visitors may only execute pre-reviewed synthetic fixtures by default.
    # Do not turn this on without a separate authentication and abuse-control layer.
    if os.environ.get("PUBLIC_SIMULATION_ENABLED", "0") != "1":
        raise HTTPException(status_code=403, detail="custom simulations are disabled on public service")
    payload = await read_contract(request, SimulationRequest, max_bytes=MAX_PUBLIC_BODY)

    # Anonymous/public traffic gets a smaller compute envelope than the domain maximums.
    if (
        len(payload.scenario.nodes) > 64
        or len(payload.scenario.roads) > 96
        or len(payload.scenario.personas) > 24
        or len(payload.plan.intents) > 32
        or sum(len(r.closures) for r in payload.scenario.roads) > 96
        or sum(len(i.route) for i in payload.plan.intents) > 384
    ):
        raise HTTPException(status_code=422, detail="public simulation limits exceeded")

    return simulate(payload.scenario, payload.plan)


@app.get("/v1/demo/{kind}", response_model=SimulationResult)
def run_demo(kind: Literal["blocked", "repaired"]) -> SimulationResult:
    return simulate(*_fixture("plan.json" if kind == "blocked" else "repaired_plan.json"))


@app.get("/v1/showcase", response_model=ShowcaseEvidence)
def showcase() -> ShowcaseEvidence:
    return _showcase()


# --- visitor challenge: the rule policy, no model ------------------------------------


@lru_cache(maxsize=256)
def _challenge(
    key: str, closed: tuple[str, ...], restrictions: tuple[ShelterRestriction, ...] = ()
) -> ChallengeRun:
    # The rule policy is deterministic: one answer per world, kept for the next visitor.
    # The cache is bounded, so the work any number of visitors can cause is bounded too.
    world, plan = _closed_world(key, closed, restrictions)
    trace = asyncio.run(run_agent(world, plan, RulePolicy()))
    return ChallengeRun(scenario=key, closed_roads=closed, world=world, trace=trace)


@app.post("/v1/agent/challenge", response_model=ChallengeRun)
async def challenge(request: Request) -> ChallengeRun:
    """Close roads of a fixed scenario or keep residents out of shelters; the rule-policy agent responds."""
    payload = await read_contract(request, ChallengeRequest, max_bytes=MAX_LIVE_BODY)
    return await asyncio.to_thread(
        _challenge,
        payload.scenario,
        tuple(sorted(payload.closed_roads)),
        _sorted_restrictions(payload.restrictions),
    )


# --- live Gemini, through the private service (live_agent.py) ------------------------


@app.get("/v1/agent/live", response_model=LiveStatus)
def live_status() -> LiveStatus:
    return live_agent.status()


@app.post("/v1/agent/live", response_model=LiveRun)
async def live_run(request: Request) -> LiveRun:
    """Run Gemini (private service, Vertex AI) on a fixed scenario; Core re-verifies the answer."""
    payload = await read_contract(request, LiveRunRequest, max_bytes=MAX_LIVE_BODY)
    closed = tuple(sorted(payload.closed_roads))
    world, plan = _closed_world(payload.scenario, closed, _sorted_restrictions(payload.restrictions))
    return await live_agent.run_live(
        payload.scenario,
        world,
        plan,
        trace_header=request.headers.get("x-cloud-trace-context"),
        closed_roads=closed,
    )


@app.post("/v1/agent/read", response_model=LiveReading)
async def live_read(request: Request) -> LiveReading:
    """Gemini (private service) reads a one-line report and names the roads it closes."""
    payload = await read_contract(request, LiveReadRequest, max_bytes=MAX_READ_BODY)
    inputs = _reviewed_inputs(payload.scenario)
    if inputs is None:
        raise HTTPException(status_code=422, detail="unknown scenario")
    scenario, _ = inputs
    return await live_agent.read_live(
        payload.scenario,
        road_labels(scenario),
        payload.report,
        trace_header=request.headers.get("x-cloud-trace-context"),
    )


# --- care notes: the residents' circumstances, the sweep and Gemini's reading --------


@app.get("/v1/agent/care", response_model=CareSet)
def care_notes(
    scenario: str = "river_district", pattern: int = Query(default=0, ge=0, lt=PATTERNS)
) -> CareSet:
    """The fictional residents' notes and the shelters' facilities that Gemini reads.

    pattern 0 is the scenario's own residents; the others put another group from the
    scenario's pool of residents in the same homes.
    """
    care = _care_set(scenario, pattern)
    if care is None:
        raise HTTPException(status_code=404, detail="no care notes for this scenario")
    return care


def _care_sweep(key: str) -> CareSweep | None:
    # Deterministic and rule-only (about a second): computed once, kept for every visitor.
    # The lock makes visitors who arrive together wait for that one computation.
    if not _known(key):
        return None
    with SWEEP_LOCK:
        return _compute_care_sweep(key)


@lru_cache(maxsize=4)
def _compute_care_sweep(key: str) -> CareSweep | None:
    files, inputs = _care_files(key), _reviewed_inputs(key)
    if files is None or inputs is None:
        return None
    care, pool = files
    if pool is None:
        return None
    scenario, plan = inputs
    return sweep(scenario, plan, [pattern_avoids(care, pool, k) for k in range(PATTERNS)])


@app.get("/v1/agent/care/sweep", response_model=CareSweep)
async def care_sweep(scenario: str = "river_district") -> CareSweep:
    """Every group of residents under the reviewer's restrictions, and which changes to a
    shelter would let a plan serve more of them. No model is called."""
    result = await asyncio.to_thread(_care_sweep, scenario)
    if result is None:
        raise HTTPException(status_code=404, detail="no resident pool for this scenario")
    return result


@app.post("/v1/agent/care", response_model=LiveCareReading)
async def live_care(request: Request) -> LiveCareReading:
    """Gemini (private service) reads the residents' notes and names the shelters each must avoid."""
    payload = await read_contract(request, LiveCareRequest, max_bytes=MAX_CARE_BODY)
    care = _care_set(payload.scenario, payload.pattern)
    if care is None:
        raise HTTPException(status_code=422, detail="no care notes for this scenario")
    edits = {edit.actor_id: edit.note for edit in payload.notes}
    known = {resident.actor_id for resident in care.residents}
    if not set(edits) <= known or len(edits) != len(payload.notes):
        raise HTTPException(status_code=422, detail="unknown or repeated resident")
    residents = tuple(
        resident.model_copy(update={"note": edits[resident.actor_id]})
        if resident.actor_id in edits
        else resident
        for resident in care.residents
    )
    care_request = CareReadRequest(residents=residents, shelters=care.shelters)
    return await live_agent.read_care(
        payload.scenario, care_request, trace_header=request.headers.get("x-cloud-trace-context")
    )


# --- HTML pages ----------------------------------------------------------------------


@app.get("/lab", include_in_schema=False)
def interactive_lab() -> FileResponse:
    """Static, browser-only synthetic GA laboratory (no public compute endpoint)."""
    return FileResponse(STATIC_DIR / "evac-lab.html", media_type="text/html")


@app.get("/", include_in_schema=False, response_model=None)
def index(request: Request) -> Response:
    """The play page. Old slide links (/?shot=N) keep working by moving to /slides."""
    shot = request.query_params.get("shot")
    if shot is not None:
        target = "/slides?shot=" + shot if shot.isascii() and shot.isdigit() and len(shot) <= 2 else "/slides"
        return RedirectResponse(target, status_code=307)
    return FileResponse(STATIC_DIR / "play.html", media_type="text/html")


@app.get("/slides", include_in_schema=False)
def slides() -> FileResponse:
    """The ten submission slides (?shot=1..10)."""
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


@app.get("/api-guide", include_in_schema=False)
def api_guide() -> FileResponse:
    return FileResponse(STATIC_DIR / "api.html", media_type="text/html")
