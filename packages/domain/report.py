"""Contracts for reading a short situation report with Gemini.

A visitor types one line such as "the bridge is flooded". That text is untrusted: the
private service sends it to Gemini as data, together with the roads of one reviewed
synthetic town, and Gemini may only answer with ids of those roads. The public service
checks the ids again, and a world with those roads closed is still judged by the
Simulation Core like any other.
"""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from packages.domain.live import ClosedRoads, RunsLeft
from packages.domain.models import Contract, Identifier

MAX_REPORT_CHARS = 120
# One line of text: no control characters, newlines included.
ReportText = Annotated[str, Field(min_length=1, max_length=MAX_REPORT_CHARS, pattern=r"^[^\x00-\x1f\x7f]+$")]
Label = Annotated[str, Field(min_length=1, max_length=40, pattern=r"^[^\x00-\x1f\x7f]+$")]
RunId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


class RoadLabel(Contract):
    """A road as a person would name it: its Japanese name and the two places it joins."""

    id: Identifier
    name: Label
    between: tuple[Label, Label]


class ReportReadRequest(Contract):
    """Input of the private service's reader: the report and the only roads it may name."""

    schema_version: Literal["report-read-request/v1"] = "report-read-request/v1"
    report: ReportText
    roads: tuple[RoadLabel, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def unique_roads(self) -> Self:
        if len({road.id for road in self.roads}) != len(self.roads):
            raise ValueError("road ids must be unique")
        return self


class ReportReading(Contract):
    """Gemini's reading: the roads the report says are impassable (empty if none)."""

    schema_version: Literal["report-reading/v1"] = "report-reading/v1"
    # The same id is on the private service's Cloud Logging entry for this call.
    run_id: RunId
    model: Annotated[str, Field(min_length=1, max_length=128)]
    prompt_version: Literal["report_reader/v1"] = "report_reader/v1"
    closed_roads: ClosedRoads

    @model_validator(mode="after")
    def unique_roads(self) -> Self:
        if len(set(self.closed_roads)) != len(self.closed_roads):
            raise ValueError("a road is named once")
        return self


class LiveReadRequest(Contract):
    """A visitor's report about one of the reviewed synthetic towns."""

    schema_version: Literal["live-read-request/v1"] = "live-read-request/v1"
    scenario: Identifier
    report: ReportText


class LiveReading(Contract):
    schema_version: Literal["live-reading/v1"] = "live-reading/v1"
    scenario: Identifier
    reading: ReportReading
    duration_ms: Annotated[int, Field(ge=0, le=600_000)]
    runs_left_today: RunsLeft
