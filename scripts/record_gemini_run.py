"""Record one real Gemini agent run from the private service for public display.

PAID: the private service makes up to 6 billable Gemini calls on Vertex AI for this
run. The script refuses to start without --confirm-paid-call.

The recording is written only if replaying its decisions locally reproduces every
observation, verified result and the outcome exactly; the public service repeats
that replay before showing it. Authentication uses a short-lived identity token
from `gcloud auth print-identity-token`; no key is stored.

It runs only after scripts/diagnose_gemini.py (one call) succeeded on the same
revision and model, and not again there after a failed recording, so the same cause
is not paid for repeatedly (packages/services/gemini_attempts.py).

  python scripts/record_gemini_run.py \\
    --service-url https://disaster-agentic-orchestrator-XXXX.asia-northeast1.run.app \\
    --confirm-paid-call
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.agents.loop import replay_matches  # noqa: E402
from packages.domain.agent import AgentTrace, RecordedAgentRun  # noqa: E402
from packages.domain.io import load_contract, parse_contract  # noqa: E402
from packages.domain.models import Plan, Scenario  # noqa: E402
from packages.services import gemini_attempts  # noqa: E402

FIXTURE = ROOT / "data" / "demo_area" / "blocked_route"
DEFAULT_OUT = ROOT / "data" / "evidence" / "recorded_gemini_run.json"
MAX_RESPONSE_BYTES = 1_048_576


SERVICE_PREFIX = "disaster-agentic-orchestrator-"


class _NoRedirect(HTTPRedirectHandler):
    """urllib would forward the Authorization header to a redirect target; refuse instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirect)


def service_origin(url: str) -> str:
    # The identity token is general purpose: send it only to the private agent service.
    parts = urlsplit(url.rstrip("/"))
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host.startswith(SERVICE_PREFIX)
        or not host.endswith(".run.app")
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError(
            f"use the private Cloud Run service origin (https://{SERVICE_PREFIX}….run.app)"
        )
    return f"https://{parts.netloc}"


def _call(url: str, token: str, body: dict[str, Any] | None = None) -> bytes:
    request = Request(  # noqa: S310 - origin validated by service_origin()
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    # The private service answers within its Cloud Run timeout (60 s).
    with _OPENER.open(request, timeout=75) as response:  # 3xx raises HTTPError, never followed
        raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return raw


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--service-url", required=True, type=service_origin)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--confirm-paid-call", action="store_true", help="acknowledge billable Gemini calls")
    parser.add_argument(
        "--attempts-log", type=Path, default=gemini_attempts.DEFAULT_LOG, help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("this run makes billable Gemini calls; pass --confirm-paid-call to proceed")

    token = subprocess.run(
        ["gcloud", "auth", "print-identity-token"], check=True, capture_output=True, text=True
    ).stdout.strip()
    health = json.loads(_call(args.service_url + "/health", token))
    if health.get("config") != "ready":
        print("private service is not configured; nothing was called", file=sys.stderr)
        return 2
    host = urlsplit(args.service_url).hostname or ""
    revision = str(health.get("revision", "unknown"))
    model = f"{health.get('model', 'unknown')}@{health.get('vertex_location', 'unknown')}"
    profile = str(health.get("profile", "default"))
    refusal = gemini_attempts.record_refusal(
        gemini_attempts.load(args.attempts_log), host, revision, model, profile
    )
    if refusal:
        print(f"not run: {refusal}; nothing was called.", file=sys.stderr)
        return 3

    def remember(ok: bool, run_id: str | None) -> None:
        gemini_attempts.append(
            gemini_attempts.Attempt(
                kind="record",
                host=host,
                revision=revision,
                model=model,
                purpose="decision",
                ok=ok,
                at=gemini_attempts.now_utc().isoformat(),
                run_id=run_id,
                profile=profile,
            ),
            args.attempts_log,
        )

    scenario = load_contract(FIXTURE / "scenario.json", Scenario)
    plan = load_contract(FIXTURE / "plan.json", Plan)
    body = {
        "schema_version": "agent-run-request/v1",
        "scenario": scenario.model_dump(mode="json"),
        "plan": plan.model_dump(mode="json"),
    }
    try:
        raw = _call(args.service_url + "/v1/agent/run", token, body)
    except HTTPError as exc:
        # The run may have reached Gemini before failing (e.g. a 504): count it as a failure.
        remember(False, None)
        print(f"agent run failed: HTTP {exc.code}; not recorded.", file=sys.stderr)
        return 2
    trace = parse_contract(raw, AgentTrace)
    if trace.policy != "vertex-gemini":
        print("response is not a Gemini run; not recorded", file=sys.stderr)
        return 2
    if trace.outcome == "halted_model_error":
        # A failed call is not evidence of Gemini's decisions. The private service logged
        # the failing request's exception class, HTTP status and time (gemini_call_failed).
        remember(False, trace.run_id)
        print(f"Gemini call failed (run_id={trace.run_id}); not recorded.", file=sys.stderr)
        print(
            "See why (no retry on this revision and model until the cause is fixed):\n  gcloud logging read "
            f'\'resource.type="cloud_run_revision" AND jsonPayload.run_id="{trace.run_id}"\' '
            "--project physical-ai-guardian --limit 20 --format json",
            file=sys.stderr,
        )
        return 2
    if not asyncio.run(replay_matches(scenario, plan, trace)):
        print("replay does not reproduce the response; not recorded", file=sys.stderr)
        return 2

    recorded = RecordedAgentRun(
        recorded_at=datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        service_revision=str(health.get("revision", "unknown"))[:64],
        trace=trace,
    )
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(recorded.model_dump_json(indent=2) + "\n", encoding="utf-8")
    remember(True, trace.run_id)
    actions = " → ".join(step.decision.action if step.decision else "error" for step in trace.steps)
    print(f"model={trace.model} calls={trace.model_calls} outcome={trace.outcome}")
    print(f"decisions: {actions}")
    print(f"saved: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
