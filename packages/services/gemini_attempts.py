"""Local record of paid Gemini attempts made from Cloud Shell, so a failure is not retried blindly.

scripts/diagnose_gemini.py and scripts/record_gemini_run.py write one line per attempt
(no prompt, answer or token: the service, its revision and model, the outcome, the
exception class and HTTP status). The rules they enforce:

- A diagnostic that failed is not repeated against the same revision, model and
  request profile: the same request would fail the same way. Deploy a fix (a new
  revision), switch the model or try another profile first. A transient failure
  (HTTP 429/500/503) may be retried once, ten minutes later. A 504 is not transient
  here: Vertex AI gave up at the deadline the request itself set (a hang).
- A recording (up to 6 calls) needs a successful decision diagnostic (1 call) on the
  same revision, model and profile, and is not repeated there after a failure.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

TRANSIENT_STATUS = frozenset({429, 500, 503})
TRANSIENT_WAIT = timedelta(minutes=10)
MAX_ATTEMPTS_PER_TARGET = 2
DEFAULT_LOG = Path(__file__).resolve().parents[2] / "artifacts" / "diagnostics" / "gemini-attempts.jsonl"

Kind = Literal["diagnostic", "record"]


@dataclass(frozen=True)
class Attempt:
    kind: Kind
    host: str
    revision: str
    model: str
    purpose: str
    ok: bool
    at: str  # UTC, ISO 8601
    run_id: str | None = None
    error_type: str | None = None
    status_code: int | None = None
    profile: str = "default"

    def same_target(
        self, kind: Kind, host: str, revision: str, model: str, purpose: str, profile: str
    ) -> bool:
        return (self.kind, self.host, self.revision, self.model, self.purpose, self.profile) == (
            kind,
            host,
            revision,
            model,
            purpose,
            profile,
        )


def now_utc() -> datetime:
    return datetime.now(UTC)


def load(path: Path = DEFAULT_LOG) -> list[Attempt]:
    if not path.is_file():
        return []
    attempts = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            attempts.append(Attempt(**json.loads(line)))
    return attempts


def append(attempt: Attempt, path: Path = DEFAULT_LOG) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(asdict(attempt), ensure_ascii=False) + "\n")


def diagnostic_refusal(
    attempts: list[Attempt],
    host: str,
    revision: str,
    model: str,
    purpose: str,
    profile: str,
    now: datetime,
) -> str | None:
    """Why a diagnostic must not run now, or None when it may."""
    same = [a for a in attempts if a.same_target("diagnostic", host, revision, model, purpose, profile)]
    if not same or same[-1].ok:
        return None
    last = same[-1]
    reason = f"{last.error_type or 'unknown'} (HTTP {last.status_code or '-'}, run_id {last.run_id or '-'})"
    if len(same) >= MAX_ATTEMPTS_PER_TARGET:
        return f"this revision, model and profile already failed {len(same)} times, last with {reason}"
    if last.status_code not in TRANSIENT_STATUS:
        return f"this revision, model and profile already failed with {reason}; the same request fails again"
    if now - datetime.fromisoformat(last.at) < TRANSIENT_WAIT:
        return f"a transient failure {reason} was seen; wait ten minutes before the one retry"
    return None


def record_refusal(attempts: list[Attempt], host: str, revision: str, model: str, profile: str) -> str | None:
    """Why a (6-call) recording must not run now, or None when it may."""
    # same_target's arguments after kind: (host, revision, model, purpose, profile). A
    # recording is a run of decisions, so the diagnostic it needs is a decision call.
    target = (host, revision, model, "decision", profile)
    if any(a.same_target("record", *target) and not a.ok for a in attempts):
        return "a recording already failed on this revision, model and profile"
    diagnostics = [a for a in attempts if a.same_target("diagnostic", *target)]
    if not diagnostics or not diagnostics[-1].ok:
        return "diagnose this revision, model and profile first (scripts/diagnose_gemini.py, one call)"
    return None
