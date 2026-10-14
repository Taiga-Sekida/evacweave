"""Strict evidence contract for the public synthetic demonstration."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.agent import MAX_PROBES, AgentTrace, ContingencyPlan, FaultProbe, RecordedAgentRun
from packages.domain.models import Contract, Identifier, Plan, Scenario, SimulationResult


class ScenarioRun(Contract):
    """One more synthetic world, run by the same rule-policy agent from a baseline plan."""

    key: Identifier
    title: Annotated[str, Field(min_length=1, max_length=40)]
    scenario: Scenario
    baseline_plan: Plan
    agent_trace: AgentTrace

    @model_validator(mode="after")
    def trace_matches_inputs(self) -> Self:
        if self.agent_trace.policy != "rule-policy":
            raise ValueError("public scenario runs use the rule policy")
        if (self.agent_trace.scenario_id, self.agent_trace.initial_plan_id) != (
            self.scenario.id,
            self.baseline_plan.id,
        ):
            raise ValueError("scenario run trace does not belong to its inputs")
        return self


class ShowcaseEvidence(Contract):
    schema_version: Literal["evacweave-showcase/v2"] = "evacweave-showcase/v2"
    evidence_kind: Literal["synthetic-deterministic"] = "synthetic-deterministic"
    # Keeping these explicit prevents screenshots from implying an LLM ran live.
    live_ai_used: Literal[False] = False
    scenario: Scenario
    baseline_plan: Plan
    baseline_result: SimulationResult
    repaired_plan: Plan
    repaired_result: SimulationResult
    repair_method: Literal["bounded-deterministic-search"] = "bounded-deterministic-search"
    probes: tuple[FaultProbe, ...] = Field(max_length=MAX_PROBES)
    contingencies: tuple[ContingencyPlan, ...] = Field(max_length=MAX_PROBES)
    agent_trace: AgentTrace
    more_runs: tuple[ScenarioRun, ...] = Field(default=(), max_length=4)
    recorded_ai_run: RecordedAgentRun | None = None
    recorded_ai_run_status: Literal["absent", "replay_verified", "rejected"] = "absent"

    @model_validator(mode="after")
    def validate_verdicts(self) -> Self:
        if self.baseline_result.status != "FAIL":
            raise ValueError("evidence baseline must fail")
        if self.repaired_result.status != "PASS":
            raise ValueError("evidence repair must pass")
        if (
            self.baseline_result.scenario_id != self.scenario.id
            or self.repaired_result.scenario_id != self.scenario.id
            or self.baseline_result.plan_id != self.baseline_plan.id
            or self.repaired_result.plan_id != self.repaired_plan.id
        ):
            raise ValueError("showcase input/result identity mismatch")
        if self.agent_trace.policy != "rule-policy":
            raise ValueError("the public demo runs the rule policy, never a live model")
        if self.agent_trace.initial_result != self.baseline_result:
            raise ValueError("agent trace must start from the baseline")
        if (self.recorded_ai_run is not None) != (self.recorded_ai_run_status == "replay_verified"):
            raise ValueError("only a replay-verified AI run is shown")
        return self
