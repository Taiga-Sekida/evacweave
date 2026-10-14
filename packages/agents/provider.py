"""Provider boundary for later AI integration; fixtures are not AI responses.

Serves the Gemma path (one MoveIntent per resident), which is not wired into the
deployed services (roadmap).
"""

from dataclasses import dataclass
from typing import Protocol

from packages.domain.models import MoveIntent, Persona, Plan, Scenario, SimulationResult
from packages.simulation.engine import simulate


class DecisionProvider(Protocol):
    async def propose(self, scenario: Scenario, persona: Persona) -> MoveIntent: ...


@dataclass(frozen=True)
class FixtureDecisionProvider:
    plan: Plan

    async def propose(self, scenario: Scenario, persona: Persona) -> MoveIntent:
        candidates = tuple(i for i in self.plan.intents if i.actor_id == persona.id)
        if len(candidates) != 1:
            raise ValueError("fixture provider requires exactly one intent per persona")
        return candidates[0]


async def evaluate_provider(scenario: Scenario, provider: DecisionProvider) -> SimulationResult:
    """One decision per resident. Provider errors propagate; never convert ERROR to PASS/FAIL."""
    scenario = Scenario.model_validate(scenario)
    intents = []
    for persona in sorted(scenario.personas, key=lambda p: p.id):
        intent = MoveIntent.model_validate(await provider.propose(scenario, persona))
        if intent.actor_id != persona.id:
            raise ValueError("provider cannot act as another persona")
        intents.append(intent)
    return simulate(scenario, Plan(id="provider_plan", intents=tuple(intents)))
