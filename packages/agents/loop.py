"""Guarded agent loop: observe -> decide -> act -> verify, until finish or escalate.

The loop owns all state. Each turn it derives an observation from verified state,
including the only actions allowed next, and asks a policy to pick one. A policy
(rules, Gemini, or a replay of a recorded run) cannot touch state: tools are
deterministic and every plan they produce is re-verified by the Simulation Core,
so nothing a policy says can turn FAIL into PASS. Invalid output, a disallowed
action or a model failure halts the run and is recorded (fail closed).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal, Protocol

from pydantic import ValidationError

from packages.domain.agent import (
    MAX_PROBES,
    REASONS_FOR_ACTION,
    TERMINAL_ACTIONS,
    AgentAction,
    AgentDecision,
    AgentLimits,
    AgentObservation,
    AgentStep,
    AgentTrace,
    ContingencyPlan,
    FaultProbe,
    PolicyError,
    PolicyName,
    PromptVersion,
    ReasonCode,
    RunOutcome,
    ToolEffect,
)
from packages.domain.models import Plan, Scenario, SimulationResult
from packages.redteam.probes import single_road_faults
from packages.repair.contingency import plan_contingencies
from packages.repair.planner import SearchBudgetExceeded, propose_repair
from packages.services.budget import BudgetExceeded
from packages.simulation.engine import simulate


class PolicyOutputError(ValueError):
    """A policy or reader answered, but not with a well-formed decision or reading."""


class AgentPolicy(Protocol):
    @property
    def name(self) -> PolicyName: ...

    @property
    def model(self) -> str | None: ...

    @property
    def prompt_version(self) -> PromptVersion | None: ...

    @property
    def model_calls(self) -> int: ...

    async def decide(self, observation: AgentObservation) -> AgentDecision: ...


StepListener = Callable[[AgentStep], None]

# A malformed or disallowed answer is the policy's fault; a failed or unaffordable call
# is the model's. Either way the run halts.
_HALT_OUTCOME: dict[PolicyError, RunOutcome] = {
    "invalid_output": "halted_policy_violation",
    "action_not_allowed": "halted_policy_violation",
    "model_error": "halted_model_error",
    "model_budget_exhausted": "halted_model_error",
}


@dataclass
class _State:
    scenario: Scenario
    plan: Plan
    result: SimulationResult
    repair_attempted: bool = False
    probes: tuple[FaultProbe, ...] | None = None
    contingencies: tuple[ContingencyPlan, ...] | None = None

    @property
    def single_points_of_failure(self) -> int:
        return sum(1 for probe in self.probes or () if probe.status == "FAIL")

    @property
    def contingencies_covered(self) -> int:
        return sum(1 for item in self.contingencies or () if item.covered)

    @property
    def roads_not_probed(self) -> int:
        if self.probes is None:
            return 0
        used = {road for intent in self.plan.intents for road in intent.route}
        return len(used) - len(self.probes)

    def allowed_actions(self, steps_remaining: int) -> tuple[AgentAction, ...]:
        work: tuple[AgentAction, ...] = ()
        if steps_remaining > 1:  # the last step is reserved for a terminal decision
            if self.result.status == "FAIL":
                work = () if self.repair_attempted else ("run_repair",)
            elif self.probes is None:
                work = ("run_redteam",)
            elif self.single_points_of_failure and self.contingencies is None:
                work = ("plan_contingency",)
        # A plan that failed verification can only be handed to a human.
        terminal: tuple[AgentAction, ...] = (
            ("finish", "escalate") if self.result.status == "PASS" else ("escalate",)
        )
        return work + terminal

    def observe(self, step: int, steps_remaining: int) -> AgentObservation:
        return AgentObservation(
            step=step,
            steps_remaining=steps_remaining,
            plan_id=self.plan.id,
            plan_status=self.result.status,
            residents=len(self.scenario.personas),
            evacuated=len(self.result.evacuated),
            rejection_reasons=tuple(sorted({e.reason for e in self.result.events if e.kind == "rejected"})),
            repair_attempted=self.repair_attempted,
            stress_tested=self.probes is not None,
            single_points_of_failure=self.single_points_of_failure,
            contingencies_planned=self.contingencies is not None,
            contingencies_covered=self.contingencies_covered,
            roads_not_probed=self.roads_not_probed,
            allowed_actions=self.allowed_actions(steps_remaining),
        )

    def _effect(self, status: Literal["applied", "terminal"], **extra: object) -> ToolEffect:
        """The plan and verdict as they stand, after a tool ran or at the end of the run."""
        return ToolEffect.model_validate(
            {
                "status": status,
                "plan_id": self.plan.id,
                "plan_status": self.result.status,
                "input_digest": self.result.input_digest,
                **extra,
            }
        )

    def act(self, action: AgentAction) -> ToolEffect:
        if action == "run_repair":
            self.repair_attempted = True
            try:
                repaired = propose_repair(self.scenario, self.plan)
            except SearchBudgetExceeded:
                return ToolEffect(status="search_budget_exhausted")
            if repaired is None:
                return ToolEffect(status="no_candidate")
            self.plan, self.result = repaired
            return self._effect("applied")
        if action == "run_redteam":
            # Every road the plan uses is probed up to MAX_PROBES (the contract's maximum);
            # any road beyond it is reported as not probed and keeps the outcome at residual risk.
            self.probes = single_road_faults(self.scenario, self.plan, limit=MAX_PROBES)
            return self._effect("applied", probes=self.probes, roads_not_probed=self.roads_not_probed)
        if action == "plan_contingency":
            self.contingencies = plan_contingencies(self.scenario, self.plan, self.probes or ())
            return self._effect("applied", contingencies=self.contingencies)
        # finish or escalate: no tool runs; record the state handed over.
        return self._effect("terminal")

    def outcome(self, action: AgentAction) -> RunOutcome:
        if action == "escalate":
            return "escalated"
        if self.probes is None:
            return "verified_untested"
        # A road that was never closed in the stress test is an unknown, not a pass.
        if self.roads_not_probed or self.contingencies_covered < self.single_points_of_failure:
            return "verified_with_residual_risk"
        if self.single_points_of_failure == 0:
            return "verified_robust"
        return "verified_with_contingencies"


async def _ask(
    policy: AgentPolicy, observation: AgentObservation
) -> tuple[AgentDecision | None, PolicyError | None]:
    try:
        decision = AgentDecision.model_validate(await policy.decide(observation))
    except BudgetExceeded:
        return None, "model_budget_exhausted"
    except (PolicyOutputError, ValidationError):
        return None, "invalid_output"
    except Exception:  # noqa: BLE001 - any model/transport failure halts the run, fail closed
        return None, "model_error"
    if decision.action not in observation.allowed_actions:
        return decision, "action_not_allowed"
    return decision, None


async def run_agent(
    scenario: Scenario,
    plan: Plan,
    policy: AgentPolicy,
    *,
    limits: AgentLimits | None = None,
    on_step: StepListener | None = None,
    run_id: str | None = None,
) -> AgentTrace:
    """Run the guarded loop and return the full, verifiable trace."""
    scenario = Scenario.model_validate(scenario)
    plan = Plan.model_validate(plan)
    limits = limits or AgentLimits()
    initial = simulate(scenario, plan)
    state = _State(scenario=scenario, plan=plan, result=initial)
    steps: list[AgentStep] = []
    outcome: RunOutcome | None = None

    for index in range(limits.max_steps):
        observation = state.observe(index, limits.max_steps - index)
        decision, error = await _ask(policy, observation)
        effect: ToolEffect | None = None
        if error is None and decision is not None:
            # Tools are CPU-bound but budgeted; run them off the event loop so the caller's
            # timeout and other requests stay responsive.
            effect = await asyncio.to_thread(state.act, decision.action)
        step = AgentStep(
            index=index, observation=observation, decision=decision, policy_error=error, effect=effect
        )
        steps.append(step)
        if on_step is not None:
            on_step(step)
        if error is not None:
            outcome = _HALT_OUTCOME[error]
            break
        if decision is not None and decision.action in TERMINAL_ACTIONS:
            outcome = state.outcome(decision.action)
            break

    if outcome is None:  # unreachable: the last step only allows terminal actions
        raise RuntimeError("agent loop ended without a terminal decision")
    # Hand back a plan only if it passed, and only when the policy ended the run.
    keeps_plan = outcome.startswith("verified") or (outcome == "escalated" and state.result.status == "PASS")
    return AgentTrace(
        run_id=run_id,
        policy=policy.name,
        model=policy.model,
        prompt_version=policy.prompt_version,
        limits=limits,
        scenario_id=scenario.id,
        initial_plan_id=plan.id,
        initial_result=initial,
        steps=tuple(steps),
        model_calls=policy.model_calls,
        outcome=outcome,
        final_plan=state.plan if keeps_plan else None,
        final_result=state.result if keeps_plan else None,
    )


class RulePolicy:
    """Deterministic baseline policy. Not AI; used for the public demo and as a reference."""

    name: PolicyName = "rule-policy"
    model: str | None = None
    prompt_version: PromptVersion | None = None
    model_calls: int = 0

    async def decide(self, observation: AgentObservation) -> AgentDecision:
        allowed = observation.allowed_actions
        for action in ("run_repair", "run_redteam", "plan_contingency"):
            if action in allowed:
                return AgentDecision(action=action, reason_code=REASONS_FOR_ACTION[action][0])
        fully_covered = (
            observation.stress_tested
            and observation.roads_not_probed == 0
            and observation.contingencies_covered == observation.single_points_of_failure
        )
        if "finish" in allowed and fully_covered:
            return AgentDecision(action="finish", reason_code="verified_plan_ready")
        # Untested or uncovered weaknesses are a human call, not a rule's.
        reason: ReasonCode = (
            "no_verified_plan" if observation.plan_status == "FAIL" else "needs_human_judgement"
        )
        return AgentDecision(action="escalate", reason_code=reason)


@dataclass
class ReplayPolicy:
    """Feeds back the decisions of a recorded run, in order, to re-execute it."""

    recorded: AgentTrace
    _cursor: int = field(default=0, init=False)
    _calls: int = field(default=0, init=False)

    @property
    def name(self) -> PolicyName:
        return self.recorded.policy

    @property
    def model(self) -> str | None:
        return self.recorded.model

    @property
    def prompt_version(self) -> PromptVersion | None:
        return self.recorded.prompt_version

    @property
    def model_calls(self) -> int:
        # Recounted, not copied, so a trace cannot misreport its own spend: a Gemini
        # policy pays one call per decision it is asked for (reserved before the request,
        # never refunded) unless its budget was already spent; a rule policy pays none.
        return self._calls if self.recorded.policy == "vertex-gemini" else 0

    async def decide(self, observation: AgentObservation) -> AgentDecision:
        if self._cursor >= len(self.recorded.steps):
            raise PolicyOutputError("replay asked for more decisions than were recorded")
        step = self.recorded.steps[self._cursor]
        self._cursor += 1
        if step.policy_error != "model_budget_exhausted":
            self._calls += 1
        if step.policy_error == "invalid_output":
            raise PolicyOutputError("recorded invalid output")
        if step.policy_error == "model_budget_exhausted":
            raise BudgetExceeded("recorded budget exhaustion")
        if step.decision is None:
            raise RuntimeError("recorded model error")
        return step.decision


async def replay_matches(scenario: Scenario, plan: Plan, recorded: AgentTrace) -> bool:
    """True when re-executing the recorded decisions reproduces the recorded trace exactly.

    Observations, tool effects, digests and the outcome are all recomputed here, so a
    recorded AI run can be shown publicly without trusting anything but its decisions.
    """
    replayed = await run_agent(
        scenario, plan, ReplayPolicy(recorded), limits=recorded.limits, run_id=recorded.run_id
    )
    return replayed == recorded
