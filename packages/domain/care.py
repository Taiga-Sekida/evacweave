"""Contracts of the care reading: residents' circumstances in words, read into restrictions.

A care set is reviewed synthetic data: each resident of a scenario has a short note (as a
welfare worker might write it) and each shelter a description of its facilities. Gemini
reads the notes against the facilities and names, for each resident, the shelters that
would put them at risk or fail a basic need, with a reason from a fixed list
(packages.domain.models.RestrictionReason). The restrictions become hard constraints of
the scenario, which the planner respects and the Simulation Core checks. Every resident
is fictional; no note is logged, only how many there were.
"""

from __future__ import annotations

import hashlib
from itertools import pairwise
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.live import RunsLeft
from packages.domain.models import Contract, Identifier, RestrictionReason, ShelterRestriction

# One line of text each: no control characters, newlines included.
NoteText = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[^\x00-\x1f\x7f]+$")]
FacilityText = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\x00-\x1f\x7f]+$")]
Label = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[^\x00-\x1f\x7f]+$")]
RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
MAX_RESIDENTS = 16
MAX_SHELTERS = 8
MAX_POOL = 200
# Groups of residents a visitor can load, by number. Pattern 0 is the scenario's own
# residents (care.json); the others are drawn from the resident pool (residents.json).
PATTERNS = 30
Pattern = Annotated[int, Field(ge=0, lt=PATTERNS)]
Restrictions = Annotated[tuple[ShelterRestriction, ...], Field(max_length=MAX_RESIDENTS * MAX_SHELTERS)]


class ResidentNote(Contract):
    actor_id: Identifier
    label: Label
    note: NoteText


class ShelterFacility(Contract):
    id: Identifier
    name: Label
    facilities: FacilityText


def _unique(ids: list[str]) -> bool:
    return len(set(ids)) == len(ids)


class CareSet(Contract):
    """Reviewed synthetic care data of one scenario (data/demo_area/<scenario>/care.json)."""

    schema_version: Literal["care-profiles/v1"] = "care-profiles/v1"
    scenario: Identifier
    synthetic: Literal[True] = True
    shelters: tuple[ShelterFacility, ...] = Field(min_length=1, max_length=MAX_SHELTERS)
    residents: tuple[ResidentNote, ...] = Field(min_length=1, max_length=MAX_RESIDENTS)
    # What a reviewer set from the same notes: shown when no model reads them.
    reviewed_restrictions: Restrictions = ()
    # Which group of residents this is, and how many groups there are (see care_pattern).
    pattern: Pattern = 0
    patterns: Annotated[int, Field(ge=1, le=PATTERNS)] = 1

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if not _unique([s.id for s in self.shelters]) or not _unique([r.actor_id for r in self.residents]):
            raise ValueError("duplicate ids")
        return self


class Avoid(Contract):
    shelter_id: Identifier
    reason: RestrictionReason


class PoolResident(Contract):
    """A fictional resident in their own words, and what a reviewer set from the note.

    There is no field for origin, nationality, religion or anything else a need could be
    guessed from: a need exists only when the note states it.
    """

    id: Identifier
    label: Label
    note: NoteText
    # Every need a shelter fails, so a sweep can tell what meeting one of them changes.
    # A shelter may appear once per reason; a scenario keeps the first (one_per_pair).
    avoid: tuple[Avoid, ...] = Field(default=(), max_length=32)

    @model_validator(mode="after")
    def no_repeats(self) -> Self:
        if not _unique([f"{a.shelter_id}/{a.reason}" for a in self.avoid]):
            raise ValueError("repeated shelter and reason")
        return self


class ResidentPool(Contract):
    """Reviewed synthetic residents of a scenario (data/demo_area/<scenario>/residents.json).

    The first residents are the scenario's own (care.json), in the same order.
    """

    schema_version: Literal["resident-pool/v1"] = "resident-pool/v1"
    scenario: Identifier
    synthetic: Literal[True] = True
    residents: tuple[PoolResident, ...] = Field(min_length=1, max_length=MAX_POOL)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if not _unique([r.id for r in self.residents]):
            raise ValueError("duplicate ids")
        return self


def pattern_members(scenario: str, pool_size: int, slots: int, pattern: int) -> tuple[int, ...]:
    """Pool positions of one pattern's residents, slot by slot.

    Pattern 0 is the first residents. Any other pattern takes the pool in the order of a
    SHA-256 of (scenario, pattern, position): the same on every machine and version.
    """
    if not 0 < slots <= pool_size:
        raise ValueError("a pattern needs as many residents as slots")
    if pattern == 0:
        return tuple(range(slots))
    order = sorted(
        range(pool_size), key=lambda i: hashlib.sha256(f"{scenario}/{pattern}/{i}".encode()).digest()
    )
    return tuple(order[:slots])


# Every reason the reviewer gave for one pattern: a resident and shelter appear once per
# reason, so a sweep can lift one need and keep the others. A scenario's restrictions
# keep only the first reason for each pair (one_per_pair).
Avoids = tuple[ShelterRestriction, ...]


def _pattern_residents(care: CareSet, pool: ResidentPool, pattern: int) -> list[tuple[str, PoolResident]]:
    """Each of the scenario's places (actor ids) and the pool resident there in this pattern."""
    slots = [resident.actor_id for resident in care.residents]
    members = pattern_members(care.scenario, len(pool.residents), len(slots), pattern)
    return [(actor, pool.residents[index]) for actor, index in zip(slots, members, strict=True)]


def pattern_avoids(care: CareSet, pool: ResidentPool, pattern: int) -> Avoids:
    """Every reason the reviewer gave for one pattern, in the scenario's places.

    A resident and shelter appear once per reason: this is what a sweep needs, not what a
    scenario holds (see one_per_pair).
    """
    residents = _pattern_residents(care, pool, pattern)
    shelters = {shelter.id for shelter in care.shelters}
    avoids: list[ShelterRestriction] = []
    for actor, resident in residents:
        for avoid in resident.avoid:
            if avoid.shelter_id not in shelters:
                raise ValueError("the pool names a shelter the scenario does not have")
            avoids.append(
                ShelterRestriction(actor_id=actor, shelter_id=avoid.shelter_id, reason=avoid.reason)
            )
    return tuple(avoids)


def one_per_pair(avoids: Avoids) -> tuple[ShelterRestriction, ...]:
    """The first reason given for each resident and shelter, sorted: what a scenario holds."""
    first: dict[tuple[str, str], ShelterRestriction] = {}
    for avoid in avoids:
        first.setdefault((avoid.actor_id, avoid.shelter_id), avoid)
    return tuple(first[key] for key in sorted(first))


def care_pattern(care: CareSet, pool: ResidentPool, pattern: int) -> CareSet:
    """The scenario's care set with another group of residents in the same places.

    The places (actor ids, homes on the map) stay; who lives there, their notes and the
    reviewer's restrictions come from the pool.
    """
    residents = tuple(
        ResidentNote(actor_id=actor, label=resident.label, note=resident.note)
        for actor, resident in _pattern_residents(care, pool, pattern)
    )
    return CareSet(
        scenario=care.scenario,
        shelters=care.shelters,
        residents=residents,
        reviewed_restrictions=one_per_pair(pattern_avoids(care, pool, pattern)),
        pattern=pattern,
        patterns=PATTERNS,
    )


class CareReadRequest(Contract):
    """Input of the private service's care reader: the notes and the shelters they may name."""

    schema_version: Literal["care-read-request/v1"] = "care-read-request/v1"
    residents: tuple[ResidentNote, ...] = Field(min_length=1, max_length=MAX_RESIDENTS)
    shelters: tuple[ShelterFacility, ...] = Field(min_length=1, max_length=MAX_SHELTERS)

    @model_validator(mode="after")
    def unique_ids(self) -> Self:
        if not _unique([s.id for s in self.shelters]) or not _unique([r.actor_id for r in self.residents]):
            raise ValueError("duplicate ids")
        return self


class CareReading(Contract):
    """Gemini's reading: shelters to keep each resident away from, and why."""

    schema_version: Literal["care-reading/v1"] = "care-reading/v1"
    # The same id is on the private service's Cloud Logging entry for this call.
    run_id: RunId
    model: Annotated[str, Field(min_length=1, max_length=128)]
    # v1 knew six reasons; v2 adds language, food and a quiet room.
    prompt_version: Literal["care_reader/v1", "care_reader/v2"] = "care_reader/v2"
    restrictions: Restrictions

    @model_validator(mode="after")
    def unique_pairs(self) -> Self:
        if not _unique([f"{r.actor_id}/{r.shelter_id}" for r in self.restrictions]):
            raise ValueError("one restriction per resident and shelter")
        return self


class NoteEdit(Contract):
    actor_id: Identifier
    note: NoteText


class LiveCareRequest(Contract):
    """A visitor's request: read the scenario's notes, some of them rewritten by the visitor."""

    schema_version: Literal["live-care-request/v1"] = "live-care-request/v1"
    scenario: Identifier
    pattern: Pattern = 0
    notes: tuple[NoteEdit, ...] = Field(default=(), max_length=MAX_RESIDENTS)


class LiveCareReading(Contract):
    schema_version: Literal["live-care-reading/v1"] = "live-care-reading/v1"
    scenario: Identifier
    reading: CareReading
    duration_ms: Annotated[int, Field(ge=0, le=600_000)]
    runs_left_today: RunsLeft


# --- the sweep: every pattern under the reviewer's restrictions -----------------------------

SweepCause = Literal["no_shelter", "capacity", "route"]


class PatternCheck(Contract):
    """One pattern, under the reviewer's restrictions and the scenario's roads as they are.

    verified: the Simulation Core accepted a plan that gives everyone a shelter. If not,
    the cause: residents no shelter can take ("no_shelter", in `residents`), shelters too
    small for everyone who can only go to them ("capacity", in `shelters`), or neither,
    so the routes or times ran out ("route").
    """

    pattern: Pattern
    residents_with_needs: Annotated[int, Field(ge=0, le=MAX_RESIDENTS)]
    restrictions: Annotated[int, Field(ge=0, le=MAX_RESIDENTS * MAX_SHELTERS)]
    verified: bool
    cause: SweepCause | None = None
    residents: tuple[Identifier, ...] = Field(default=(), max_length=MAX_RESIDENTS)
    shelters: tuple[Identifier, ...] = Field(default=(), max_length=MAX_SHELTERS)

    @model_validator(mode="after")
    def cause_only_when_failed(self) -> Self:
        if self.verified != (self.cause is None):
            raise ValueError("a cause is given exactly when no plan was verified")
        return self


SweepChange = Literal["meet_need", "add_capacity"]


class SweepFix(Contract):
    """One change to one shelter, and the failed patterns it alone turns into verified plans.

    meet_need: the shelter meets `reason` (the reviewer's restrictions for it there are
    lifted). add_capacity: the shelter takes CAPACITY_STEP more residents.
    """

    shelter_id: Identifier
    change: SweepChange
    reason: RestrictionReason | None = None
    rescued: tuple[Pattern, ...] = Field(min_length=1, max_length=PATTERNS)

    @model_validator(mode="after")
    def reason_only_for_needs(self) -> Self:
        if (self.change == "meet_need") != (self.reason is not None):
            raise ValueError("a reason is given exactly for meet_need")
        return self


class SweepStep(Contract):
    """One change of a cumulative sequence, and how many patterns verify once it is made."""

    shelter_id: Identifier
    change: SweepChange
    reason: RestrictionReason | None = None
    verified: Annotated[int, Field(ge=0, le=PATTERNS)]

    @model_validator(mode="after")
    def reason_only_for_needs(self) -> Self:
        if (self.change == "meet_need") != (self.reason is not None):
            raise ValueError("a reason is given exactly for meet_need")
        return self


class CareSweep(Contract):
    schema_version: Literal["care-sweep/v1"] = "care-sweep/v1"
    scenario: Identifier
    synthetic: Literal[True] = True
    capacity_step: Annotated[int, Field(ge=1, le=16)]
    patterns: tuple[PatternCheck, ...] = Field(min_length=1, max_length=PATTERNS)
    verified: Annotated[int, Field(ge=0, le=PATTERNS)]
    # Single changes, most rescued first.
    fixes: tuple[SweepFix, ...] = Field(default=(), max_length=16)
    # Changes made one after another, each the one that rescues the most patterns left.
    steps: tuple[SweepStep, ...] = Field(default=(), max_length=8)

    @model_validator(mode="after")
    def counts_agree(self) -> Self:
        if self.verified != sum(p.verified for p in self.patterns):
            raise ValueError("verified must count the verified patterns")
        counts = [self.verified, *(step.verified for step in self.steps)]
        if any(after <= before for before, after in pairwise(counts)):
            raise ValueError("every step must rescue at least one pattern")
        return self
