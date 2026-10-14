"""Run Gemini through the public live endpoints, the way a judge would.

PAID: the public service asks the private agent service to run Gemini on Vertex AI:
one agent run (at most 6 billable calls) and one report reading (1 call). The script
refuses to start without --confirm-paid-call.

The public service replays the returned decisions through the Simulation Core and
answers only if they reproduce every result; this script repeats that replay locally,
saves the run under artifacts/evidence/ and prints the Cloud Logging query for it.
Then it has Gemini read a sample report about the district and prints the roads read.
No credentials are needed: it calls the public URL like any visitor.

  python scripts/check_live_ai.py \\
    --public-url https://disaster-agentic-mvp-XXXX.asia-northeast1.run.app \\
    --confirm-paid-call
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.agents.loop import replay_matches  # noqa: E402
from packages.domain.io import load_contract, parse_contract  # noqa: E402
from packages.domain.live import LiveRun, LiveStatus  # noqa: E402
from packages.domain.models import Plan, Scenario  # noqa: E402
from packages.domain.report import LiveReading  # noqa: E402

FIXTURE = ROOT / "data" / "demo_area" / "blocked_route"
OUT_DIR = ROOT / "artifacts" / "evidence"
PUBLIC_PREFIX = "disaster-agentic-mvp-"
PRIVATE_SERVICE = "disaster-agentic-orchestrator"
PROJECT = "physical-ai-guardian"
MAX_RESPONSE_BYTES = 1_048_576
SAMPLE_REPORT = "橋が冠水して渡れないと消防から連絡"
EXPECTED_ROADS = ("river_bridge",)


def public_origin(url: str) -> str:
    parts = urlsplit(url.strip().rstrip("/"))
    host = parts.hostname or ""
    hosted = parts.scheme == "https" and host.startswith(PUBLIC_PREFIX) and host.endswith(".run.app")
    local = parts.scheme == "http" and host in {"127.0.0.1", "localhost"}
    if (
        not (hosted or local)
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError(
            f"use the public Cloud Run origin (https://{PUBLIC_PREFIX}….run.app) or local http"
        )
    return f"{parts.scheme}://{parts.netloc}"


def _http(url: str, body: dict[str, Any] | None = None) -> bytes:
    request = Request(  # noqa: S310 - origin validated by public_origin()
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="GET" if body is None else "POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    with urlopen(request, timeout=90) as response:  # noqa: S310
        raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return raw


def logging_query(run_id: str) -> str:
    return (
        "gcloud logging read "
        f'\'resource.type="cloud_run_revision" AND resource.labels.service_name="{PRIVATE_SERVICE}" '
        f'AND jsonPayload.run_id="{run_id}"\' --project {PROJECT} --limit 20 --format json'
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--public-url", required=True, type=public_origin)
    parser.add_argument("--confirm-paid-call", action="store_true", help="acknowledge billable Gemini calls")
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("this run makes billable Gemini calls; pass --confirm-paid-call to proceed")

    status = parse_contract(_http(args.public_url + "/v1/agent/live"), LiveStatus)
    if not status.enabled:
        print("live Gemini is off on this service; nothing was called", file=sys.stderr)
        return 2
    if status.runs_left_today < 2:
        print(
            "fewer than 2 live requests are left today (a run and a reading); nothing was called",
            file=sys.stderr,
        )
        return 2
    try:
        raw = _http(args.public_url + "/v1/agent/live", {"scenario": "base"})
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        print(f"live run refused: HTTP {exc.code} {detail}", file=sys.stderr)
        return 1
    run = parse_contract(raw, LiveRun)
    trace = run.trace
    scenario = load_contract(FIXTURE / "scenario.json", Scenario)
    plan = load_contract(FIXTURE / "plan.json", Plan)
    if not asyncio.run(replay_matches(scenario, plan, trace)):
        print("local replay does not reproduce the answer", file=sys.stderr)
        return 2

    run_id = trace.run_id or "unknown"  # the LiveRun contract requires one
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out = OUT_DIR / f"live-run-{run_id}.json"
    out.write_text(run.model_dump_json(indent=2) + "\n", encoding="utf-8")
    actions = " → ".join(step.decision.action if step.decision else "error" for step in trace.steps)
    print(f"model={trace.model} calls={trace.model_calls} outcome={trace.outcome} {run.duration_ms} ms")
    print(f"decisions: {actions}")
    print(f"runs left today: {run.runs_left_today}")
    print(f"saved: {out}")
    print("Cloud Logging entries of this run:")
    print("  " + logging_query(run_id))
    if trace.outcome == "halted_model_error":
        print("Gemini call failed inside the run; see the log entries above.", file=sys.stderr)
        return 3
    return check_reading(args.public_url)


def check_reading(origin: str) -> int:
    """Gemini reads one sample report about the district; prints the roads it named."""
    try:
        raw = _http(origin + "/v1/agent/read", {"scenario": "river_district", "report": SAMPLE_REPORT})
    except HTTPError as exc:
        detail = exc.read(4096).decode("utf-8", errors="replace")
        print(f"report reading refused: HTTP {exc.code} {detail}", file=sys.stderr)
        return 4
    reading = parse_contract(raw, LiveReading).reading
    roads = ", ".join(reading.closed_roads) or "no road"
    print(f"reading: {SAMPLE_REPORT} -> {roads} (model={reading.model}, run_id={reading.run_id})")
    print("  " + logging_query(reading.run_id))
    if reading.closed_roads != EXPECTED_ROADS:
        # A reading is a model judgement, not a verified result: report it, do not fail.
        print("note: Gemini read the sample differently than expected (river_bridge)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
