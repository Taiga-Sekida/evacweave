"""Shared contracts. Integer simulation seconds and immutable nested collections.

A *persona* is a resident: every `actor_id` names a `Persona.id` (an intent naming
anyone else is rejected), and `AgentObservation.residents` counts personas.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
Second = Annotated[int, Field(ge=0, le=86400)]
Verdict = Literal["PASS", "FAIL"]
# Closures one road may carry. A road at the cap takes no stress-test closure (with_road_closed).
MAX_CLOSURES_PER_ROAD = 64


class Contract(BaseModel):
    """Base of every contract: strict and immutable.

    Unknown fields, type coercion (such as "1" for 1), NaN and Infinity are rejected, and
    an instance cannot be changed. revalidate_instances: an instance handed in where a
    contract is expected is validated again, so one built with model_construct (which
    skips every check) cannot bypass them.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, allow_inf_nan=False, revalidate_instances="always"
    )


class Closure(Contract):
    start_s: Second
    end_s: Second

    @model_validator(mode="after")
    def positive_interval(self) -> Self:
        if self.end_s <= self.start_s:
            raise ValueError("closure must have positive duration")
        return self


class Road(Contract):
    id: Identifier
    source: Identifier
    target: Identifier
    travel_time_s: Annotated[int, Field(gt=0, le=86400)]
    closures: tuple[Closure, ...] = Field(default=(), max_length=MAX_CLOSURES_PER_ROAD)

    @model_validator(mode="after")
    def not_self_loop(self) -> Self:
        if self.source == self.target:
            raise ValueError("self-loop roads are not supported")
        return self


class Shelter(Contract):
    id: Identifier
    node: Identifier
    capacity: Annotated[int, Field(ge=0, le=10000)]
    opens_at_s: Second = 0


class Persona(Contract):
    id: Identifier
    start_node: Identifier


# Why a resident must not be sent to a shelter. A fixed, reviewed list: whatever reads a
# resident's circumstances (Gemini, a person) can only pick from these.
RestrictionReason = Literal[
    "step_free_access",  # steps or a slope the resident cannot manage
    "location_privacy",  # someone the resident must be hidden from may be there
    "night_safety",  # no lighting, patrol or women's space for someone at risk at night
    "infant_care",  # no place to nurse or change a baby
    "medical_power",  # no power for a medical device
    "pet_companion",  # the resident's animal is not allowed in
    # Needs the resident states (never inferred from a name, origin or language spoken):
    "language_support",  # no guidance in a language the resident understands, no interpreter
    "dietary_needs",  # no stored food the resident can eat (an allergy, a religious rule)
    "quiet_room",  # no small quiet room (for prayer, or to calm down away from crowds)
]


class ShelterRestriction(Contract):
    """A shelter this resident must not be sent to, and why."""

    actor_id: Identifier
    shelter_id: Identifier
    reason: RestrictionReason


class Scenario(Contract):
    schema_version: Literal["evacuation-scenario/v1"] = "evacuation-scenario/v1"
    id: Identifier
    dataset_version: Identifier
    synthetic: Literal[True] = True
    deadline_s: Annotated[int, Field(gt=0, le=86400)]
    nodes: tuple[Identifier, ...] = Field(min_length=1, max_length=256)
    roads: tuple[Road, ...] = Field(max_length=512)
    shelters: tuple[Shelter, ...] = Field(min_length=1, max_length=64)
    personas: tuple[Persona, ...] = Field(min_length=1, max_length=256)
    # Hard constraints the Core checks: a resident sent to one of these is rejected.
    restrictions: tuple[ShelterRestriction, ...] = Field(default=(), max_length=1024)

    @model_serializer(mode="wrap")
    def _omit_no_restrictions(self, handler: SerializerFunctionWrapHandler) -> Any:
        # A scenario without restrictions serialises as before, so its input digest and
        # what services that predate restrictions accept stay the same.
        data = handler(self)
        if isinstance(data, dict) and not self.restrictions:
            data.pop("restrictions", None)
        return data

    @model_validator(mode="after")
    def consistent_references(self) -> Self:
        road_ids = tuple(r.id for r in self.roads)
        shelter_ids = tuple(s.id for s in self.shelters)
        actor_ids = tuple(p.id for p in self.personas)
        if any(len(set(ids)) != len(ids) for ids in (self.nodes, road_ids, shelter_ids, actor_ids)):
            raise ValueError("duplicate identifiers")
        nodes = set(self.nodes)
        if any(r.source not in nodes or r.target not in nodes for r in self.roads):
            raise ValueError("unknown road node")
        if any(s.node not in nodes for s in self.shelters):
            raise ValueError("unknown shelter node")
        if any(p.start_node not in nodes for p in self.personas):
            raise ValueError("unknown persona node")
        actors, shelters = set(actor_ids), set(shelter_ids)
        if any(r.actor_id not in actors or r.shelter_id not in shelters for r in self.restrictions):
            raise ValueError("unknown restriction reference")
        pairs = [(r.actor_id, r.shelter_id) for r in self.restrictions]
        if len(set(pairs)) != len(pairs):
            raise ValueError("duplicate restriction")
        return self


class MoveIntent(Contract):
    action: Literal["move"] = "move"
    actor_id: Identifier
    shelter_id: Identifier
    departure_s: Second = 0
    route: tuple[Identifier, ...] = Field(min_length=1, max_length=256)


class Plan(Contract):
    schema_version: Literal["evacuation-plan/v1"] = "evacuation-plan/v1"
    id: Identifier
    intents: tuple[MoveIntent, ...] = Field(max_length=1024)


class Event(Contract):
    time_s: Second
    actor_id: Identifier
    kind: Literal["departed", "arrived", "rejected"]
    reason: str
    node: Identifier | None = None


class Position(Contract):
    actor_id: Identifier
    node: Identifier


class SimulationResult(Contract):
    schema_version: Literal["evacuation-result/v1"] = "evacuation-result/v1"
    engine_version: Literal["static-network-v1"] = "static-network-v1"
    simulation_only: Literal[True] = True
    scenario_id: Identifier
    plan_id: Identifier
    input_digest: str
    status: Verdict
    evacuated: tuple[Identifier, ...]
    not_evacuated: tuple[Identifier, ...]
    final_locations: tuple[Position, ...]
    events: tuple[Event, ...]
