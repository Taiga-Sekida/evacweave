"""The guarded agent loop: behaviour, guardrails, fail-closed halts and replay."""

import asyncio
import json
import random

import pytest

from packages.agents.loop import PolicyOutputError, ReplayPolicy, RulePolicy, replay_matches, run_agent
from packages.agents.orchestrator import GeminiAgentPolicy
from packages.domain.agent import (
    REASONS_FOR_ACTION,
    AgentDecision,
    AgentLimits,
    AgentTrace,
)
from packages.domain.models import Plan, Scenario
from packages.redteam.probes import with_road_closed
from packages.services.budget import AttemptBudget, BudgetExceeded
from packages.simulation.engine import simulate

ACTIONS = tuple(REASONS_FOR_ACTION)


def run(scenario, plan, policy, **kwargs) -> AgentTrace:
    return asyncio.run(run_agent(scenario, plan, policy, **kwargs))


class ScriptedPolicy:
    """Stands in for Gemini: answers from a script, raising when told to."""

    name = "vertex-gemini"
    model = "gemini-test"
    prompt_version = "orchestrator/v2"

    def __init__(self, *answers):
        self.answers = list(answers)
        self.observations = []
        self.model_calls = 0

    async def decide(self, observation):
        self.observations.append(observation)
        self.model_calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        action, reason = answer
        return AgentDecision(action=action, reason_code=reason)


def _all_closed(scenario: Scenario) -> Scenario:
    for road in scenario.roads:
        scenario = with_road_closed(scenario, road.id)
    return scenario


def test_rule_policy_repairs_stress_tests_and_covers_weaknesses(scenario, plan):
    trace = run(scenario, plan, RulePolicy())
    assert [s.decision.action for s in trace.steps] == [
        "run_repair",
        "run_redteam",
        "plan_contingency",
        "finish",
    ]
    assert trace.outcome == "verified_with_contingencies"
    assert trace.policy == "rule-policy" and trace.model is None and trace.model_calls == 0
    # The loop's claims are the Core's: re-simulate everything the trace reports.
    assert trace.initial_result == simulate(scenario, plan)
    assert trace.final_result == simulate(scenario, trace.final_plan)
    assert trace.final_result.status == "PASS"
    repaired = trace.steps[0].effect
    assert (repaired.status, repaired.plan_status) == ("applied", "PASS")
    assert repaired.input_digest == trace.final_result.input_digest
    assert [p.road_id for p in trace.steps[1].effect.probes] == ["road_b1", "road_b2"]
    assert all(c.covered for c in trace.steps[2].effect.contingencies)


def test_observations_offer_only_state_appropriate_actions(scenario, plan):
    trace = run(scenario, plan, RulePolicy())
    assert [s.observation.allowed_actions for s in trace.steps] == [
        ("run_repair", "escalate"),
        ("run_redteam", "finish", "escalate"),
        ("plan_contingency", "finish", "escalate"),
        ("finish", "escalate"),
    ]
    assert [s.observation.plan_status for s in trace.steps] == ["FAIL", "PASS", "PASS", "PASS"]


def test_run_is_deterministic_and_does_not_mutate_inputs(scenario, plan):
    before = scenario.model_dump_json(), plan.model_dump_json()
    assert run(scenario, plan, RulePolicy()) == run(scenario, plan, RulePolicy())
    assert before == (scenario.model_dump_json(), plan.model_dump_json())


def test_passing_plan_skips_repair(scenario, repaired):
    trace = run(scenario, repaired, RulePolicy())
    assert [s.decision.action for s in trace.steps] == ["run_redteam", "plan_contingency", "finish"]
    assert trace.final_plan == repaired


def test_unrepairable_plan_is_escalated_to_a_human(scenario, plan):
    trace = run(_all_closed(scenario), plan, RulePolicy())
    assert [(s.decision.action, s.effect.status) for s in trace.steps] == [
        ("run_repair", "no_candidate"),
        ("escalate", "terminal"),
    ]
    assert trace.steps[1].decision.reason_code == "no_verified_plan"
    assert trace.steps[1].observation.allowed_actions == ("escalate",)
    assert trace.outcome == "escalated"
    assert trace.final_plan is None and trace.final_result is None


def test_repair_search_budget_exhaustion_is_recorded_and_escalated(scenario, plan, monkeypatch):
    import packages.repair.planner as planner

    monkeypatch.setattr(planner, "MAX_SEARCH_STATES", 1)
    trace = run(scenario, plan, RulePolicy())
    assert trace.steps[0].effect.status == "search_budget_exhausted"
    assert trace.outcome == "escalated"


def test_last_step_is_reserved_for_a_terminal_decision(scenario, repaired):
    trace = run(scenario, repaired, RulePolicy(), limits=AgentLimits(max_steps=2))
    assert trace.steps[-1].observation.allowed_actions == ("finish", "escalate")
    # Weaknesses were found but there was no step left to cover them: hand over.
    assert trace.steps[-1].decision.reason_code == "needs_human_judgement"
    assert trace.outcome == "escalated"
    assert trace.final_result.status == "PASS"  # the verified candidate goes to the human


def test_single_step_failing_plan_can_only_escalate(scenario, plan):
    trace = run(scenario, plan, RulePolicy(), limits=AgentLimits(max_steps=1))
    assert trace.steps[0].observation.allowed_actions == ("escalate",)
    assert trace.outcome == "escalated"


def test_finishing_a_failed_plan_halts_the_run(scenario, plan):
    policy = ScriptedPolicy(("finish", "verified_plan_ready"))
    trace = run(scenario, plan, policy)
    step = trace.steps[0]
    assert step.policy_error == "action_not_allowed"
    assert step.decision.action == "finish"  # the attempt is kept for audit
    assert step.effect is None
    assert trace.outcome == "halted_policy_violation"
    assert trace.final_plan is None and trace.initial_result.status == "FAIL"


@pytest.mark.parametrize(
    ("error", "code", "outcome"),
    [
        (PolicyOutputError("bad json"), "invalid_output", "halted_policy_violation"),
        (TimeoutError(), "model_error", "halted_model_error"),
        (RuntimeError("503"), "model_error", "halted_model_error"),
        (BudgetExceeded("spent"), "model_budget_exhausted", "halted_model_error"),
    ],
)
def test_policy_failures_halt_without_side_effects(scenario, plan, error, code, outcome):
    trace = run(scenario, plan, ScriptedPolicy(("run_repair", "plan_failed_verification"), error))
    assert [s.policy_error for s in trace.steps] == [None, code]
    assert trace.steps[-1].effect is None
    assert trace.outcome == outcome
    assert trace.final_plan is None


def test_policy_sees_only_derived_observations(scenario, plan):
    policy = ScriptedPolicy(
        ("run_repair", "plan_failed_verification"),
        ("run_redteam", "check_robustness"),
        ("finish", "accept_residual_risk"),
    )
    trace = run(scenario, plan, policy)
    assert policy.observations == [s.observation for s in trace.steps]
    assert trace.outcome == "verified_with_residual_risk"
    assert trace.model_calls == 3


def test_step_listener_sees_every_step(scenario, plan):
    seen = []
    trace = run(scenario, plan, RulePolicy(), on_step=seen.append)
    assert tuple(seen) == trace.steps


@pytest.mark.parametrize("seed", range(40))
def test_no_policy_can_turn_fail_into_a_verified_outcome(scenario, plan, seed):
    """An adversarial policy picking arbitrary actions never breaks the invariants."""
    rng = random.Random(seed)

    class Chaos:
        name, model, prompt_version, model_calls = "rule-policy", None, None, 0

        async def decide(self, observation):
            action = rng.choice(ACTIONS)
            return AgentDecision(action=action, reason_code=rng.choice(REASONS_FOR_ACTION[action]))

    world = _all_closed(scenario) if seed % 2 else scenario
    trace = run(world, plan, Chaos())
    AgentTrace.model_validate_json(trace.model_dump_json())  # contract still holds
    if trace.outcome.startswith("verified"):
        assert trace.final_result == simulate(world, trace.final_plan)
        assert trace.final_result.status == "PASS"
    for step in trace.steps:
        if step.decision and step.policy_error is None:
            assert step.decision.action in step.observation.allowed_actions


def test_replay_reproduces_a_recorded_run(scenario, plan):
    recorded = run(
        scenario,
        plan,
        ScriptedPolicy(
            ("run_repair", "plan_failed_verification"),
            ("run_redteam", "check_robustness"),
            ("plan_contingency", "cover_single_points_of_failure"),
            ("finish", "verified_plan_ready"),
        ),
    )
    assert recorded.policy == "vertex-gemini"
    assert asyncio.run(replay_matches(scenario, plan, recorded))


def test_replay_reproduces_a_recorded_halt(scenario, plan):
    recorded = run(scenario, plan, ScriptedPolicy(("finish", "verified_plan_ready")))
    assert asyncio.run(replay_matches(scenario, plan, recorded))
    errored = run(scenario, plan, ScriptedPolicy(PolicyOutputError("x")))
    assert asyncio.run(replay_matches(scenario, plan, errored))


def _tampered(trace: AgentTrace, edit) -> AgentTrace:
    data = json.loads(trace.model_dump_json())
    edit(data)
    return AgentTrace.model_validate_json(json.dumps(data))


@pytest.mark.parametrize(
    "edit",
    [
        # An observation the loop would not have produced.
        lambda d: d["steps"][1]["observation"].update(evacuated=0),
        # A tool result that the Core would not reproduce.
        lambda d: d["steps"][0]["effect"].update(input_digest="0" * 64),
        # An inflated outcome.
        lambda d: d.update(outcome="verified_robust"),
    ],
)
def test_replay_detects_tampering(scenario, plan, edit):
    recorded = run(scenario, plan, RulePolicy())
    assert not asyncio.run(replay_matches(scenario, plan, _tampered(recorded, edit)))


def test_replay_recounts_model_calls_instead_of_trusting_them(scenario, plan):
    recorded = run(
        scenario,
        plan,
        ScriptedPolicy(("run_repair", "plan_failed_verification"), ("escalate", "needs_human_judgement")),
    )
    assert recorded.model_calls == 2
    assert asyncio.run(replay_matches(scenario, plan, recorded))
    understated = _tampered(recorded, lambda d: d.update(model_calls=1))
    assert not asyncio.run(replay_matches(scenario, plan, understated))


def test_replay_does_not_charge_a_call_the_budget_refused(scenario, plan):
    class Transport:
        async def generate(self, **kwargs):
            return {"action": "run_repair", "reason_code": "plan_failed_verification"}

    policy = GeminiAgentPolicy("gemini-test", Transport(), AttemptBudget(gemma_limit=0, gemini_limit=1))
    recorded = run(scenario, plan, policy)
    assert [s.policy_error for s in recorded.steps] == [None, "model_budget_exhausted"]
    assert recorded.model_calls == 1
    assert asyncio.run(replay_matches(scenario, plan, recorded))


def test_replay_checks_consequences_not_authorship(scenario, plan):
    # Replay proves that the recorded decisions lead to the recorded results. It cannot
    # prove who made a decision: a self-consistent edit of a reason code replays fine.
    recorded = run(scenario, plan, RulePolicy())
    edited = _tampered(
        recorded, lambda d: d["steps"][3]["decision"].update(reason_code="accept_residual_risk")
    )
    assert asyncio.run(replay_matches(scenario, plan, edited))


def test_replay_rejects_a_run_from_another_world(scenario, plan):
    recorded = run(scenario, plan, RulePolicy())
    assert not asyncio.run(replay_matches(_all_closed(scenario), plan, recorded))
    other_plan = Plan(id="other", intents=plan.intents)
    assert not asyncio.run(replay_matches(scenario, other_plan, recorded))


def test_replay_policy_stops_when_recorded_decisions_run_out(scenario, plan):
    recorded = run(scenario, plan, RulePolicy())
    short = recorded.model_copy(update={"steps": recorded.steps[:1]})
    policy = ReplayPolicy(short)
    asyncio.run(policy.decide(recorded.steps[0].observation))
    with pytest.raises(PolicyOutputError):
        asyncio.run(policy.decide(recorded.steps[1].observation))


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda d: d.update(policy="vertex-gemini"), "Gemini policy"),
        (lambda d: d.update(outcome="verified_robust", final_plan=None, final_result=None), "passing final"),
        (lambda d: d["steps"][0].update(index=3), "numbered"),
        (lambda d: d["steps"][0].update(effect=None), "policy error"),
        (lambda d: d["steps"][0]["decision"].update(reason_code="verified_plan_ready"), "does not justify"),
    ],
)
def test_trace_contract_rejects_incoherent_traces(scenario, plan, edit, message):
    trace = run(scenario, plan, RulePolicy())
    data = json.loads(trace.model_dump_json())
    edit(data)
    with pytest.raises(ValueError, match=message):
        AgentTrace.model_validate_json(json.dumps(data))


def _chain_world(length: int, extra_closures: int = 0):
    nodes = [f"n{i}" for i in range(length + 1)]
    roads = []
    for i in range(length):
        closures = (
            [{"start_s": 500 + 2 * k, "end_s": 501 + 2 * k} for k in range(extra_closures)] if i == 0 else []
        )
        roads.append(
            {
                "id": f"r{i:02d}",
                "source": nodes[i],
                "target": nodes[i + 1],
                "travel_time_s": 1,
                "closures": closures,
            }
        )
    scenario = Scenario.model_validate_json(
        json.dumps(
            {
                "id": "chain",
                "dataset_version": "v",
                "deadline_s": 100,
                "nodes": nodes,
                "roads": roads,
                "shelters": [{"id": "s", "node": nodes[-1], "capacity": 1}],
                "personas": [{"id": "p", "start_node": "n0"}],
            }
        )
    )
    plan = Plan.model_validate_json(
        json.dumps(
            {
                "id": "chain_plan",
                "intents": [{"actor_id": "p", "shelter_id": "s", "route": [r["id"] for r in roads]}],
            }
        )
    )
    return scenario, plan


@pytest.mark.parametrize(("length", "closures", "unprobed"), [(25, 0, 1), (1, 64, 1)])
def test_unprobed_roads_are_residual_risk_not_robustness(length, closures, unprobed):
    # A plan using more roads than the probe cap, or a road that cannot take another
    # closure, must not be reported as robust or fully covered.
    scenario, plan = _chain_world(length, closures)
    finisher = ScriptedPolicy(("run_redteam", "check_robustness"), ("finish", "verified_plan_ready"))
    finisher.name, finisher.model, finisher.prompt_version = "rule-policy", None, None
    trace = run(scenario, plan, finisher)
    assert trace.steps[0].effect.roads_not_probed == unprobed
    assert trace.steps[1].observation.roads_not_probed == unprobed
    assert trace.outcome == "verified_with_residual_risk"
    assert run(scenario, plan, RulePolicy()).outcome == "escalated"  # rules hand it over


def test_tools_run_off_the_event_loop(scenario, plan, monkeypatch):
    import time

    import packages.agents.loop as loop

    monkeypatch.setattr(loop, "propose_repair", lambda *a, **k: time.sleep(0.5))

    async def timed() -> float:
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(run_agent(scenario, plan, RulePolicy()), timeout=0.05)
        return time.monotonic() - started

    # The deadline fires while the tool is still busy in its worker thread. (asyncio.run
    # itself then waits for the thread at shutdown; a server's loop keeps serving.)
    assert asyncio.run(timed()) < 0.4


def test_run_id_is_kept_and_replayed(scenario, plan):
    trace = run(scenario, plan, RulePolicy(), run_id="ab" * 16)
    assert trace.run_id == "ab" * 16
    assert asyncio.run(replay_matches(scenario, plan, trace))
