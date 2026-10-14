"""Strict contracts for AI runtime boundaries.

These models describe model-service I/O only. They do not grant an AI permission to
change simulation state or decide whether a plan is safe. All but AgentRunRequest (the
private agent service's input) serve the Gemma path, which is not wired into the
deployed services (roadmap).
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.models import (
    Contract,
    Identifier,
    MoveIntent,
    Persona,
    Plan,
    Scenario,
)

AgentRole = Literal[
    "resident",
    "shelter",
    "infrastructure",
    "information",
    "redteam",
    "repair",
]
RequestId = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")]


class AdapterSpec(Contract):
    role: AgentRole
    adapter_id: Identifier
    path: Annotated[str, Field(min_length=1, max_length=512)]


class AdapterRegistry(Contract):
    schema_version: Literal["adapter-registry/v1"] = "adapter-registry/v1"
    base_model: Annotated[str, Field(min_length=1, max_length=256)]
    processor_model: Annotated[str, Field(min_length=1, max_length=256)]
    adapters: tuple[AdapterSpec, ...] = Field(min_length=1, max_length=16)

    @model_validator(mode="after")
    def unique_roles_and_ids(self) -> Self:
        roles = [item.role for item in self.adapters]
        ids = [item.adapter_id for item in self.adapters]
        if len(roles) != len(set(roles)) or len(ids) != len(set(ids)):
            raise ValueError("adapter registry requires unique roles and adapter ids")
        return self

    def for_role(self, role: AgentRole) -> AdapterSpec:
        matches = tuple(item for item in self.adapters if item.role == role)
        if len(matches) != 1:
            raise KeyError(f"no unique adapter configured for role={role}")
        return matches[0]


class ModelIdentity(Contract):
    provider: Literal["fine-tuned-gemma"] = "fine-tuned-gemma"
    base_model: Annotated[str, Field(min_length=1, max_length=256)]
    adapter_id: Identifier


class MoveDecisionRequest(Contract):
    schema_version: Literal["move-decision-request/v1"] = "move-decision-request/v1"
    request_id: RequestId
    role: Literal["resident"] = "resident"
    scenario: Scenario
    persona: Persona

    @model_validator(mode="after")
    def persona_belongs_to_scenario(self) -> Self:
        if self.persona.id not in {item.id for item in self.scenario.personas}:
            raise ValueError("persona must belong to scenario")
        return self


class MoveDecisionResponse(Contract):
    schema_version: Literal["move-decision-response/v1"] = "move-decision-response/v1"
    request_id: RequestId
    role: Literal["resident"] = "resident"
    model: ModelIdentity
    intent: MoveIntent
    inference_ms: Annotated[int, Field(ge=0, le=600_000)]


class AgentRunRequest(Contract):
    """Input of the private agent service. Callers send the world and the plan, never a
    verdict: the service computes every SimulationResult itself."""

    schema_version: Literal["agent-run-request/v1"] = "agent-run-request/v1"
    scenario: Scenario
    plan: Plan
