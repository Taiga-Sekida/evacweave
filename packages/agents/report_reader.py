"""Gemini reads a short situation report and names the roads it says are impassable.

This is the agent's perception step: free text in, road ids out. The report is
untrusted visitor text and goes to the model as data. The answer schema is built for
each request, so the only values Gemini can return are ids of the roads offered (at
most two); the ids are checked again here and by the public service. Closing a road
changes nothing by itself: a plan for that world is still found by the agent loop and
judged by the Simulation Core.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, create_model

from packages.agents.loop import PolicyOutputError
from packages.agents.orchestrator import (
    CallObserver,
    StructuredGenerationTransport,
    call_timeout,
    observed_generate,
)
from packages.domain.live import MAX_CLOSED_ROADS
from packages.domain.report import ReportReadRequest
from packages.prompts.loader import load_prompt
from packages.services.budget import AttemptBudget

PROMPT_VERSION = "report_reader/v1"


def reading_schema(road_ids: tuple[str, ...]) -> type[BaseModel]:
    """The answer schema Gemini receives: a short list whose items are an enum of road_ids."""
    road: Any = Literal.__getitem__(road_ids)
    return create_model(
        "RoadsReported",
        __doc__="Roads the report says cannot be used now, copied from the roads list.",
        closed_roads=(
            list[road],
            Field(max_length=MAX_CLOSED_ROADS, description="At most two road ids; empty if none."),
        ),
    )


def report_prompt(request: ReportReadRequest) -> str:
    return json.dumps(
        {
            "report": request.report,
            "roads": [
                {"id": road.id, "name": road.name, "between": list(road.between)} for road in request.roads
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_reading(payload: Mapping[str, Any], offered: tuple[str, ...]) -> tuple[str, ...]:
    """The road ids of a structured answer, or PolicyOutputError: nothing else is accepted.

    The schema cannot forbid a repeated id ("the bridge... the bridge is closed"), so a
    repeat is folded into one; an id that was not offered is refused.
    """
    roads = payload.get("closed_roads") if isinstance(payload, Mapping) else None
    if not isinstance(roads, list) or len(roads) > MAX_CLOSED_ROADS:
        raise PolicyOutputError("Gemini reading must list at most two roads")
    if not all(isinstance(road, str) and road in offered for road in roads):
        raise PolicyOutputError("Gemini reading names a road it was not offered")
    return tuple(sorted(set(roads)))


@dataclass
class GeminiReportReader:
    model: str
    transport: StructuredGenerationTransport
    budget: AttemptBudget
    # Thinking tokens count against this limit: matching free text to a town's roads
    # needs more room than picking one of a few actions.
    max_output_tokens: int = 1024
    timeout_s: float = 25.0
    deadline: float | None = None  # time.monotonic(); see GeminiAgentPolicy
    on_call: CallObserver | None = None

    @property
    def model_calls(self) -> int:
        return self.budget.snapshot()["gemini_used"]

    async def read(self, request: ReportReadRequest) -> tuple[str, ...]:
        offered = tuple(road.id for road in request.roads)
        timeout = call_timeout(self.timeout_s, self.deadline)
        # Reserve before network I/O; an uncertain outcome is never refunded.
        self.budget.consume("gemini")
        try:
            payload = await observed_generate(
                self.transport,
                purpose="report_read",
                model=self.model,
                timeout_s=timeout,
                on_call=self.on_call,
                system_instruction=load_prompt("report_reader", "v1"),
                prompt=report_prompt(request),
                response_schema=reading_schema(offered),
                max_output_tokens=self.max_output_tokens,
            )
        except json.JSONDecodeError as exc:  # text, not JSON; a cut-off answer is OutputTruncated
            raise PolicyOutputError("Gemini reading is not valid JSON") from exc
        return parse_reading(payload, offered)
