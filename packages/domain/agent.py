"""Contracts for the guarded agent loop.

A policy (rules or Gemini) only *chooses* among the actions the loop allows for the
current state. Tools are deterministic, and every plan they produce is re-verified
by the Simulation Core. The trace records each observation, decision and verified
effect so that a run can be audited and replayed.

`run_redteam` is the stress test: single-road fault probes (single_road_faults, one
FaultProbe per road the plan uses). A probe that FAILs is a *single point of failure*.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.models import Contract, Identifier, Plan, Second, SimulationResult, Verdict

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
# Roads one stress test closes (one probe each); contingencies follow failed probes.
MAX_PROBES = 24

AgentAction = Literal["run_repair", "run_redteam", "plan_contingency", "finish", "escalate"]
ReasonCode = Literal[
    "plan_failed_verification",
    "check_robustness",
    "cover_single_points_of_failure",
    "verified_plan_ready",
    "accept_residual_risk",
    "no_verified_plan",
    "needs_human_judgement",
]
TERMINAL_ACTIONS: frozenset[AgentAction] = frozenset({"finish", "escalate"})
REASONS_FOR_ACTION: dict[AgentAction, tuple[ReasonCode, ...]] = {
    "run_repair": ("plan_failed_verification",),
    "run_redteam": ("check_robustness",),
    "plan_contingency": ("cover_single_points_of_failure",),
    "finish": ("verified_plan_ready", "accept_residual_risk"),
    "escalate": ("no_verified_plan", "needs_human_judgement"),
}

PolicyName = Literal["rule-policy", "vertex-gemini"]
PromptVersion = Literal["orchestrator/v2"]
PolicyError = Literal["invalid_output", "action_not_allowed", "model_error", "model_budget_exhausted"]
RunOutcome = Literal[
    "verified_robust",
    "verified_with_contingencies",
    "verified_with_residual_risk",
    "verified_untested",
    "escalated",
    "halted_policy_violation",
    "halted_model_error",
]


class FaultProbe(Contract):
    """A passing plan re-run with one additional full-window road closure."""

    road_id: Identifier
    status: Verdict
    not_evacuated: tuple[Identifier, ...]
    rejection_reasons: tuple[str, ...]
    input_digest: Digest


class ContingencyPlan(Contract):
    """A fallback plan for one single point of failure, verified under that fault."""

    road_id: Identifier
    reason: Literal["verified_alternative", "no_verified_plan", "search_budget_exhausted"]
    plan: Plan | None = None
    input_digest: Digest | None = None
    last_arrival_s: Second | None = None

    @property
    def covered(self) -> bool:
        return self.reason == "verified_alternative"

    @model_validator(mode="after")
    def plan_matches_reason(self) -> Self:
        complete = None not in (self.plan, self.input_digest, self.last_arrival_s)
        if complete != self.covered:
            raise ValueError("only a verified alternative carries a plan and digest")
        return self


class AgentLimits(Contract):
    max_steps: Annotated[int, Field(ge=1, le=8)] = 6
    max_model_calls: Annotated[int, Field(ge=0, le=8)] = 6


class AgentDecision(Contract):
    schema_version: Literal["agent-decision/v1"] = "agent-decision/v1"
    action: AgentAction
    reason_code: ReasonCode

    @model_validator(mode="after")
    def reason_fits_action(self) -> Self:
        if self.reason_code not in REASONS_FOR_ACTION[self.action]:
            raise ValueError(f"reason {self.reason_code} does not justify {self.action}")
        return self


class AgentObservation(Contract):
    """Everything a policy may see. Derived from verified state, never from a policy."""

    step: Annotated[int, Field(ge=0, le=7)]
    steps_remaining: Annotated[int, Field(ge=1, le=8)]
    plan_id: Identifier
    plan_status: Verdict
    residents: Annotated[int, Field(ge=1)]
    evacuated: Annotated[int, Field(ge=0)]
    rejection_reasons: tuple[str, ...]
    repair_attempted: bool
    stress_tested: bool
    single_points_of_failure: Annotated[int, Field(ge=0)]
    contingencies_planned: bool
    contingencies_covered: Annotated[int, Field(ge=0)]
    # Roads the plan uses that the stress test did not close (probe cap, closure cap).
    roads_not_probed: Annotated[int, Field(ge=0)] = 0
    allowed_actions: tuple[AgentAction, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent_counts(self) -> Self:
        if self.evacuated > self.residents:
            raise ValueError("more residents evacuated than exist")
        if self.contingencies_covered > self.single_points_of_failure:
            raise ValueError("more contingencies covered than weaknesses found")
        if self.plan_status == "FAIL" and "finish" in self.allowed_actions:
            raise ValueError("a failing plan can never be finished")
        return self


class ToolEffect(Contract):
    """What the chosen action did, as verified by the Simulation Core."""

    status: Literal["applied", "no_candidate", "search_budget_exhausted", "terminal"]
    plan_id: Identifier | None = None
    plan_status: Verdict | None = None
    input_digest: Digest | None = None
    probes: tuple[FaultProbe, ...] = Field(default=(), max_length=MAX_PROBES)
    roads_not_probed: Annotated[int, Field(ge=0)] = 0
    contingencies: tuple[ContingencyPlan, ...] = Field(default=(), max_length=MAX_PROBES)


class AgentStep(Contract):
    index: Annotated[int, Field(ge=0, le=7)]
    observation: AgentObservation
    decision: AgentDecision | None = None
    policy_error: PolicyError | None = None
    effect: ToolEffect | None = None

    @model_validator(mode="after")
    def decided_or_failed(self) -> Self:
        if (self.policy_error is None) != (self.effect is not None):
            raise ValueError("a step either has an effect or records a policy error")
        if self.policy_error == "action_not_allowed" and self.decision is None:
            raise ValueError("a disallowed action must be recorded")
        return self


class AgentTrace(Contract):
    schema_version: Literal["agent-trace/v1"] = "agent-trace/v1"
    # Set by the private service; the same id is on every Cloud Logging entry of the run.
    run_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")] | None = None
    policy: PolicyName
    model: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    prompt_version: PromptVersion | None = None
    limits: AgentLimits
    scenario_id: Identifier
    initial_plan_id: Identifier
    initial_result: SimulationResult
    steps: tuple[AgentStep, ...] = Field(min_length=1, max_length=8)
    model_calls: Annotated[int, Field(ge=0, le=8)]
    outcome: RunOutcome
    final_plan: Plan | None = None
    final_result: SimulationResult | None = None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if (self.policy == "vertex-gemini") != (self.model is not None and self.prompt_version is not None):
            raise ValueError("only a Gemini policy names a model and prompt version")
        if [step.index for step in self.steps] != list(range(len(self.steps))):
            raise ValueError("steps must be numbered from zero without gaps")
        if len(self.steps) > self.limits.max_steps or self.model_calls > self.limits.max_model_calls:
            raise ValueError("trace exceeds its own limits")
        if (self.final_plan is None) != (self.final_result is None):
            raise ValueError("final plan and result come together")
        if self.outcome.startswith("verified") and (
            self.final_result is None or self.final_result.status != "PASS"
        ):
            raise ValueError("a verified outcome requires a passing final result")
        if (
            self.final_plan is not None
            and self.final_result is not None
            and (self.final_result.plan_id != self.final_plan.id)
        ):
            raise ValueError("final result does not belong to final plan")
        return self


class RecordedAgentRun(Contract):
    """A real Gemini run captured from the private service for public, replayable display."""

    schema_version: Literal["recorded-agent-run/v1"] = "recorded-agent-run/v1"
    recorded_at: Annotated[str, Field(pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")]
    service_revision: Annotated[str, Field(min_length=1, max_length=64)]
    trace: AgentTrace

    @model_validator(mode="after")
    def records_gemini(self) -> Self:
        if self.trace.policy != "vertex-gemini":
            raise ValueError("only Gemini runs are recorded as AI evidence")
        return self
