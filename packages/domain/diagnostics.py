"""Contracts of the private service's one-call Gemini diagnostic.

PAID: a diagnostic sends at most one Gemini request, the same request shape as the
agent loop's first decision (or as a report reading). It answers why a live run fails
without exposing anything: which stage failed, the exception class, the HTTP status
when there is one, and how long each stage took. It never returns or logs the prompt,
the model's answer or a credential.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.models import Contract

RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
Millis = Annotated[int, Field(ge=0, le=600_000)]
ErrorType = Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
DiagnosticPurpose = Literal["decision", "report_read"]
# Request profiles of packages/agents/orchestrator.py (REQUEST_PROFILES); a test keeps them equal.
ProfileName = Literal["default", "minimal", "json"]


class GeminiDiagnosticRequest(Contract):
    schema_version: Literal["gemini-diagnostic-request/v1"] = "gemini-diagnostic-request/v1"
    # The caller states that it accepts one billable call; anything else is a 422.
    confirm_paid_call: Literal[True]
    purpose: DiagnosticPurpose = "decision"
    # None: the profile the service is configured with (GEMINI_PROFILE).
    profile: ProfileName | None = None


class GeminiDiagnostic(Contract):
    schema_version: Literal["gemini-diagnostic/v1"] = "gemini-diagnostic/v1"
    run_id: RunId  # the same id is on this diagnostic's Cloud Logging entries
    revision: Annotated[str, Field(min_length=1, max_length=64)]
    model: Annotated[str, Field(min_length=1, max_length=128)]
    location: Annotated[str, Field(min_length=1, max_length=64)]
    profile: ProfileName
    purpose: DiagnosticPurpose
    ok: bool  # a well-formed answer came back
    # prepare: SDK import, credentials, client. call: the request. answer: it came back malformed.
    failed_stage: Literal["prepare", "call", "answer"] | None
    error_type: ErrorType | None
    status_code: Annotated[int, Field(ge=100, le=599)] | None
    prepare_ms: Millis
    call_ms: Millis | None  # None when no request was sent
    model_calls: Annotated[int, Field(ge=0, le=1)]
    production_call_timeout_ms: Millis
    diagnostics_left_today: Annotated[int, Field(ge=0, le=100)]

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.ok != (self.failed_stage is None):
            raise ValueError("a diagnostic either succeeds or names the stage that failed")
        if (self.call_ms is None) != (self.model_calls == 0):
            raise ValueError("a call time is reported exactly when a request was sent")
        if self.ok and self.error_type is not None:
            raise ValueError("a successful diagnostic carries no error")
        return self
