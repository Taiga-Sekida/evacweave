"""Gemini reads residents' circumstances and names the shelters each must avoid, and why.

This is the agent's perception of people: notes in words (a wheelchair, a baby, an
abuser who may shelter nearby, a dog) in, restrictions out. The notes are data, never
instructions. The answer schema is built for each request, so Gemini can only name the
residents and shelters offered and a reason from the fixed list; the answer is checked
again here and by the public service. A restriction changes nothing by itself: the
planner keeps residents out of those shelters and the Simulation Core rejects any plan
that does not.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, get_args

from pydantic import BaseModel, Field, create_model

from packages.agents.loop import PolicyOutputError
from packages.agents.orchestrator import (
    CallObserver,
    StructuredGenerationTransport,
    call_timeout,
    observed_generate,
)
from packages.domain.care import CareReadRequest
from packages.domain.models import RestrictionReason, ShelterRestriction
from packages.prompts.loader import load_prompt
from packages.services.budget import AttemptBudget

PROMPT_VERSION = "care_reader/v2"
REASONS: tuple[str, ...] = get_args(RestrictionReason)


def care_schema(actor_ids: tuple[str, ...], shelter_ids: tuple[str, ...]) -> type[BaseModel]:
    """The answer schema Gemini receives: items whose fields are enums of the ids offered.

    The list has no length limit here. Vertex AI turns a schema into a decoding grammar,
    and Google documents long array limits, above all on nested objects, as a cause of
    one with too many states. With a limit of 36 the request was refused with 400 before
    any answer, while the same calls with short limits went through. parse_care bounds
    the list instead.
    """
    actor: Any = Literal.__getitem__(actor_ids)
    shelter: Any = Literal.__getitem__(shelter_ids)
    reason: Any = Literal.__getitem__(REASONS)
    item = create_model(
        "Restriction",
        __doc__="A shelter this resident must not be sent to, and the reason.",
        actor_id=(actor, ...),
        shelter_id=(shelter, ...),
        reason=(reason, ...),
    )
    return create_model(
        "CareRead",
        __doc__="Shelters each resident must avoid, from their notes and the shelters' facilities.",
        restrictions=(list[item], Field(description="Empty if no note states a need.")),
    )


def care_prompt(request: CareReadRequest) -> str:
    return json.dumps(
        {
            "residents": [{"id": r.actor_id, "label": r.label, "note": r.note} for r in request.residents],
            "shelters": [{"id": s.id, "name": s.name, "facilities": s.facilities} for s in request.shelters],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def parse_care(
    payload: Mapping[str, Any], actors: tuple[str, ...], shelters: tuple[str, ...]
) -> tuple[ShelterRestriction, ...]:
    """The restrictions of a structured answer, or PolicyOutputError: nothing else is accepted.

    The schema cannot forbid naming the same resident and shelter twice, so the first
    reason given for a pair is kept; anything not offered is refused.
    """
    items = payload.get("restrictions") if isinstance(payload, Mapping) else None
    if not isinstance(items, list) or len(items) > len(actors) * len(shelters):
        raise PolicyOutputError("Gemini care reading must be a bounded list")
    seen: dict[tuple[str, str], ShelterRestriction] = {}
    for item in items:
        if not isinstance(item, Mapping) or set(item) != {"actor_id", "shelter_id", "reason"}:
            raise PolicyOutputError("Gemini care reading has a malformed item")
        actor, shelter, reason = item["actor_id"], item["shelter_id"], item["reason"]
        if actor not in actors or shelter not in shelters or reason not in REASONS:
            raise PolicyOutputError("Gemini care reading names something it was not offered")
        restriction = ShelterRestriction(actor_id=actor, shelter_id=shelter, reason=reason)
        seen.setdefault((actor, shelter), restriction)
    return tuple(seen[key] for key in sorted(seen))


@dataclass
class GeminiCareReader:
    model: str
    transport: StructuredGenerationTransport
    budget: AttemptBudget
    # Thinking tokens count against this limit, and one answer covers every resident.
    max_output_tokens: int = 8192
    timeout_s: float = 25.0
    deadline: float | None = None  # time.monotonic(); see GeminiAgentPolicy
    on_call: CallObserver | None = None

    @property
    def model_calls(self) -> int:
        return self.budget.snapshot()["gemini_used"]

    async def read(self, request: CareReadRequest) -> tuple[ShelterRestriction, ...]:
        actors = tuple(r.actor_id for r in request.residents)
        shelters = tuple(s.id for s in request.shelters)
        timeout = call_timeout(self.timeout_s, self.deadline)
        # Reserve before network I/O; an uncertain outcome is never refunded.
        self.budget.consume("gemini")
        try:
            payload = await observed_generate(
                self.transport,
                purpose="care_read",
                model=self.model,
                timeout_s=timeout,
                on_call=self.on_call,
                system_instruction=load_prompt("care_reader", "v2"),
                prompt=care_prompt(request),
                response_schema=care_schema(actors, shelters),
                max_output_tokens=self.max_output_tokens,
            )
        except json.JSONDecodeError as exc:  # text, not JSON; a cut-off answer is OutputTruncated
            raise PolicyOutputError("Gemini care reading is not valid JSON") from exc
        return parse_care(payload, actors, shelters)
