"""Contracts of the public live-Gemini endpoint (GET/POST /v1/agent/live).

A visitor picks one of the fixed synthetic scenarios by key and may close up to two of
its roads (directly, or through a report read by Gemini: packages/domain/report.py).
The answer is a Gemini trace from the private service that the public service has
replayed through the Simulation Core before returning it.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.agent import AgentTrace
from packages.domain.models import Contract, Identifier, Scenario, ShelterRestriction

RunsLeft = Annotated[int, Field(ge=0, le=1000)]
# A visitor may close at most two roads of a reviewed scenario: a small, finite set of
# worlds (1 + n + n(n-1)/2 per scenario), so every answer can be cached.
MAX_CLOSED_ROADS = 2
ClosedRoads = Annotated[tuple[Identifier, ...], Field(max_length=MAX_CLOSED_ROADS)]
# Shelters residents must avoid (read from their notes by Gemini, or set by a reviewer).
# They are checked against the scenario when the world is built; nothing else trusts them.
WorldRestrictions = Annotated[tuple[ShelterRestriction, ...], Field(max_length=64)]


class LiveRunRequest(Contract):
    schema_version: Literal["live-agent-request/v1"] = "live-agent-request/v1"
    scenario: Identifier
    closed_roads: ClosedRoads = ()
    restrictions: WorldRestrictions = ()


class LiveStatus(Contract):
    schema_version: Literal["live-agent-status/v1"] = "live-agent-status/v1"
    enabled: bool
    busy: bool
    daily_limit: RunsLeft
    runs_left_today: RunsLeft


class LiveRun(Contract):
    schema_version: Literal["live-agent-run/v1"] = "live-agent-run/v1"
    scenario: Identifier
    trace: AgentTrace
    # Replay proves the decisions lead to these results. Authorship rests on the IAM
    # channel to the private service; the trace itself is not signed.
    verification: Literal["replay_verified"] = "replay_verified"
    duration_ms: Annotated[int, Field(ge=0, le=600_000)]
    runs_left_today: RunsLeft
    closed_roads: ClosedRoads = ()

    @model_validator(mode="after")
    def is_a_gemini_run(self) -> Self:
        if self.trace.policy != "vertex-gemini" or self.trace.run_id is None:
            raise ValueError("a live run is a Gemini trace with a run id")
        return self


class ChallengeRequest(Contract):
    """A visitor's challenge: close roads of a reviewed scenario, or keep residents out of shelters."""

    schema_version: Literal["agent-challenge-request/v1"] = "agent-challenge-request/v1"
    scenario: Identifier
    closed_roads: ClosedRoads = ()
    restrictions: WorldRestrictions = ()

    @model_validator(mode="after")
    def changes_something(self) -> Self:
        if not self.closed_roads and not self.restrictions:
            raise ValueError("close a road or restrict a shelter")
        return self


class ChallengeRun(Contract):
    """The rule-policy agent's answer in the visitor's world, every result from the Core."""

    schema_version: Literal["agent-challenge/v1"] = "agent-challenge/v1"
    scenario: Identifier
    closed_roads: ClosedRoads
    world: Scenario
    trace: AgentTrace

    @model_validator(mode="after")
    def trace_of_this_world(self) -> Self:
        if self.trace.policy != "rule-policy" or self.trace.scenario_id != self.world.id:
            raise ValueError("a challenge is answered by the rule policy in the challenged world")
        return self
