"""Gemini in the agent: the decision policy, and what every Gemini call shares.

Four parts, in this order:

- the model allowlist (GEMINI_MODELS), a spend guard both services check;
- per-call plumbing shared by every Gemini caller (this policy and the report and care
  readers): the transport protocol, the cut to the run's deadline (call_timeout) and
  the logged record of each call (observed_generate, ModelCall);
- the decision policy of the guarded agent loop. Gemini sees one observation per turn
  and picks one decision from the menu the loop allows. It never sees or edits a
  verdict it could overturn: the loop rejects any action outside the menu, and
  PASS/FAIL always comes from the Simulation Core;
- the Vertex AI transport (GoogleGenAITransport), which imports the SDK lazily.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError

from packages.agents.loop import PolicyOutputError
from packages.domain.agent import (
    REASONS_FOR_ACTION,
    AgentAction,
    AgentDecision,
    AgentObservation,
    PolicyName,
    PromptVersion,
    ReasonCode,
)
from packages.prompts.loader import load_prompt
from packages.services.budget import AttemptBudget, BudgetExceeded

# --- the model allowlist --------------------------------------------------------------------

# The allowlist is a spend guard, so it only names models that Vertex AI documents as
# available through the 2026-12-01 final pitch. gemini-2.5-flash retires on 2026-10-20
# (https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/model-versions) and is
# deliberately absent. The repository is frozen after submission, so the fallbacks let an
# operator switch VERTEX_GEMINI_MODEL on the Cloud Run service without a code change.
# Both services check it: the private one before calling Vertex, the public one before
# showing a live trace.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
GEMINI_MODELS = frozenset({DEFAULT_GEMINI_MODEL, "gemini-3.7-flash", "gemini-3.8-flash"})


# --- per-call plumbing shared by every Gemini caller ----------------------------------------

# A call is not started with less than this left before the run's deadline: it could
# not finish, and it might still be billed.
MIN_CALL_S = 3.0

CallPurpose = Literal["decision", "report_read", "care_read"]


class StructuredGenerationTransport(Protocol):
    async def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        response_schema: type[BaseModel],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class ModelCall:
    """One Gemini request as the services log it.

    Only what explains a failure without exposing anything: the exception class, the
    HTTP status when the error carries one, the time taken and the model. Never the
    prompt, the visitor's report, the answer or a credential (an SDK error's message
    contains the response body, so it is not kept).
    """

    purpose: CallPurpose
    model: str
    elapsed_ms: int
    error_type: str | None = None
    status_code: int | None = None
    error_hint: str | None = None


CallObserver = Callable[[ModelCall], None]

# What a refused request (HTTP 400) is about, as fixed words: the error's message is
# never logged, only which of these it names. First match wins.
_REFUSAL_HINTS = (
    ("too many states", "schema_too_complex"),
    ("schema", "schema_refused"),
    ("thinking", "thinking_refused"),
    ("token", "token_limit_refused"),
)


def api_status_code(exc: BaseException) -> int | None:
    """The HTTP status of an API failure, if the exception carries one.

    google.genai.errors.APIError has `code`; httpx errors have `response.status_code`.
    """
    response = getattr(exc, "response", None)
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(response, "status_code", None),
    ):
        if isinstance(value, int) and not isinstance(value, bool) and 100 <= value <= 599:
            return value
    return None


def api_error_hint(exc: BaseException) -> str | None:
    """For a refused request, which part of it was refused; None for any other failure."""
    if api_status_code(exc) != 400:
        return None
    message = str(exc).lower()
    return next((hint for needle, hint in _REFUSAL_HINTS if needle in message), "other")


def call_timeout(timeout_s: float, deadline: float | None) -> float:
    """Seconds one call may take: its own limit, cut to what is left before the deadline.

    BudgetExceeded when too little is left to start a call, so none is sent (and none
    billed); the loop then halts the run and records it (model_budget_exhausted).
    """
    if deadline is None:
        return timeout_s
    left = deadline - time.monotonic()
    if left < MIN_CALL_S:
        raise BudgetExceeded("the run's deadline leaves no time for another model call")
    return min(timeout_s, left)


async def observed_generate(
    transport: StructuredGenerationTransport,
    *,
    purpose: CallPurpose,
    model: str,
    timeout_s: float,
    on_call: CallObserver | None,
    system_instruction: str,
    prompt: str,
    response_schema: type[BaseModel],
    max_output_tokens: int,
) -> Mapping[str, Any]:
    """One model request under a hard timeout, reported to on_call whether it succeeds or not."""
    started = time.monotonic()
    try:
        payload = await asyncio.wait_for(
            transport.generate(
                model=model,
                system_instruction=system_instruction,
                prompt=prompt,
                response_schema=response_schema,
                max_output_tokens=max_output_tokens,
                timeout_s=timeout_s,
            ),
            timeout=timeout_s,
        )
    except BaseException as exc:  # timeouts and cancellations included; always re-raised
        if on_call is not None:
            on_call(
                ModelCall(
                    purpose,
                    model,
                    _ms_since(started),
                    type(exc).__name__,
                    api_status_code(exc),
                    api_error_hint(exc),
                )
            )
        raise
    if on_call is not None:
        on_call(ModelCall(purpose, model, _ms_since(started)))
    return payload


def _ms_since(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


# --- the decision policy --------------------------------------------------------------------


# Response schema sent to Gemini: two enums and nothing else. Deliberately not a strict
# Contract: the SDK turns this class into the request schema (its docstring becomes the
# schema description the model reads), and plain enums are the smallest surface every
# Gemini model accepts. The answer is re-validated against the strict AgentDecision.
class GeminiChoice(BaseModel):
    """One decision: an action and one of its reason codes, both from allowed_decisions."""

    action: AgentAction
    reason_code: ReasonCode


def observation_prompt(observation: AgentObservation) -> str:
    """Serialise one turn as data.

    The caller-chosen plan id is left out: it is the one field a caller could fill with
    suggestive words. Everything else is a count, a flag or an engine/loop code.
    """
    menu = [
        {"action": action, "reason_codes": list(REASONS_FOR_ACTION[action])}
        for action in observation.allowed_actions
    ]
    return json.dumps(
        {
            "observation": observation.model_dump(mode="json", exclude={"plan_id"}),
            "allowed_decisions": menu,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


@dataclass
class GeminiAgentPolicy:
    model: str
    transport: StructuredGenerationTransport
    budget: AttemptBudget
    max_output_tokens: int = 512
    timeout_s: float = 25.0
    # time.monotonic() after which no call is sent; each call is cut to fit before it.
    deadline: float | None = None
    on_call: CallObserver | None = None

    name: PolicyName = "vertex-gemini"
    prompt_version: PromptVersion = "orchestrator/v2"

    @property
    def model_calls(self) -> int:
        return self.budget.snapshot()["gemini_used"]

    async def decide(self, observation: AgentObservation) -> AgentDecision:
        # Out of time is checked before reserving: no call is sent, so none is counted.
        timeout = call_timeout(self.timeout_s, self.deadline)
        # Reserve before network I/O; an uncertain outcome is never refunded.
        self.budget.consume("gemini")
        payload = await observed_generate(
            self.transport,
            purpose="decision",
            model=self.model,
            timeout_s=timeout,
            on_call=self.on_call,
            system_instruction=load_prompt("orchestrator", "v2"),
            prompt=observation_prompt(observation),
            response_schema=GeminiChoice,
            max_output_tokens=self.max_output_tokens,
        )
        try:
            return AgentDecision.model_validate_json(
                json.dumps(dict(payload), ensure_ascii=False, allow_nan=False)
            )
        except (TypeError, ValueError, ValidationError) as exc:
            raise PolicyOutputError("Gemini output is not a valid decision") from exc


# --- the Vertex AI transport ----------------------------------------------------------------

VERTEX_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


@dataclass(frozen=True)
class RequestProfile:
    """How a request asks Gemini for its answer. The answer is validated the same way in all.

    output "schema" sends the answer schema (enums) for constrained decoding; "json"
    asks for JSON and puts the same schema in the system instruction, for when
    schema-constrained output and thinking do not finish together.
    """

    name: str
    thinking_level: Literal["LOW", "MINIMAL"]
    output: Literal["schema", "json"]


REQUEST_PROFILES: dict[str, RequestProfile] = {
    profile.name: profile
    for profile in (
        RequestProfile("default", thinking_level="LOW", output="schema"),
        RequestProfile("minimal", thinking_level="MINIMAL", output="schema"),
        RequestProfile("json", thinking_level="LOW", output="json"),
    )
}
DEFAULT_PROFILE = "default"


def json_format_instruction(response_schema: type[BaseModel]) -> str:
    """The answer schema as an instruction, for the "json" profile (no constrained decoding)."""
    schema = json.dumps(response_schema.model_json_schema(), ensure_ascii=False, separators=(",", ":"))
    return "Answer with exactly one JSON object and nothing else, valid against this JSON Schema: " + schema


def sdk_http_options(types: Any, timeout_s: float) -> Any:
    """HTTP options for one request: the SDK gives up at the caller's own limit (and tells
    the server so, X-Server-Timeout), and makes exactly one attempt. A retry would be a
    second billable call the budget never reserved."""
    return types.HttpOptions(
        api_version="v1",
        timeout=max(1000, int(timeout_s * 1000)),
        retry_options=types.HttpRetryOptions(attempts=1),
    )


class OutputTruncated(PolicyOutputError):
    """The answer hit max_output_tokens (thinking included); logged under this class name."""


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    if reason is None:
        return None
    return str(getattr(reason, "name", reason))


@dataclass
class GoogleGenAITransport:
    project: str
    location: str = "global"
    profile: RequestProfile = REQUEST_PROFILES[DEFAULT_PROFILE]
    _client: Any = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _build_client(self) -> Any:
        """Import the SDK, load and refresh the service account's credentials, build the client.

        Blocking (imports, metadata server): run it in a thread. On a cold Cloud Run
        instance it takes seconds, which is why it is not part of any model call's timer.
        """
        import google.auth
        from google import genai
        from google.auth.transport.requests import Request as AuthRequest
        from google.genai import types

        credentials, _ = google.auth.default(scopes=list(VERTEX_SCOPES))
        credentials.refresh(AuthRequest())
        return genai.Client(
            vertexai=True,
            project=self.project,
            location=self.location,
            credentials=credentials,
            http_options=sdk_http_options(types, 30.0),
        )

    async def prepare(self) -> None:
        """Have a client with a fresh access token ready. No model call, nothing billed."""
        async with self._lock:
            if self._client is None:
                self._client = await asyncio.to_thread(self._build_client)

    async def generate(
        self,
        *,
        model: str,
        system_instruction: str,
        prompt: str,
        response_schema: type[BaseModel],
        max_output_tokens: int,
        timeout_s: float,
    ) -> Mapping[str, Any]:
        await self.prepare()
        from google.genai import types

        profile = self.profile
        config: dict[str, Any] = {
            "system_instruction": system_instruction,
            # Gemini 3.x replaces thinking_budget with thinking_level; sending both is a 400.
            "thinking_config": types.ThinkingConfig(
                thinking_level=getattr(types.ThinkingLevel, profile.thinking_level)
            ),
            "max_output_tokens": max_output_tokens,
            "response_mime_type": "application/json",
            "http_options": sdk_http_options(types, timeout_s),
            # No temperature: Gemini 3's default (1.0) is what Google recommends keeping;
            # lower values can make the model loop.
        }
        if profile.output == "schema":
            config["response_schema"] = response_schema
        else:
            config["system_instruction"] = (
                system_instruction + "\n" + json_format_instruction(response_schema)
            )
        response = await self._client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(**config),
        )
        if _finish_reason(response) == "MAX_TOKENS":
            # Thinking and answer share max_output_tokens: a cut-off answer is not JSON.
            raise OutputTruncated("Gemini stopped at the output token limit")
        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, BaseModel):
            return parsed.model_dump(mode="json")
        if isinstance(parsed, dict):
            return parsed
        text = getattr(response, "text", None)
        if not isinstance(text, str) or not text:
            raise PolicyOutputError("Gemini returned no structured output")
        decoded = json.loads(text)
        if not isinstance(decoded, dict):
            raise PolicyOutputError("Gemini structured output must be a JSON object")
        return decoded
