"""DecisionProvider backed by a private fine-tuned Gemma Cloud Run service.

Serves the Gemma path, which is not wired into the deployed services (roadmap).
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

from packages.domain.ai import MoveDecisionRequest, MoveDecisionResponse
from packages.domain.models import MoveIntent, Persona, Scenario
from packages.services.budget import AttemptBudget
from packages.services.http_json import IdentityTokenSource, JsonTransport


@dataclass
class CloudRunGemmaDecisionProvider:
    endpoint: str
    audience: str
    transport: JsonTransport
    token_source: IdentityTokenSource
    budget: AttemptBudget
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        endpoint = urlsplit(self.endpoint)
        audience = urlsplit(self.audience)
        if (
            endpoint.scheme != "https"
            or audience.scheme != "https"
            or not endpoint.hostname
            or not audience.hostname
            or endpoint.username is not None
            or endpoint.password is not None
            or audience.username is not None
            or audience.password is not None
            or endpoint.path not in {"", "/"}
            or audience.path not in {"", "/"}
            or endpoint.query
            or endpoint.fragment
            or audience.query
            or audience.fragment
        ):
            raise ValueError("endpoint and audience must be HTTPS service origins")
        if endpoint.netloc != audience.netloc:
            raise ValueError("endpoint origin must match identity-token audience")
        if not 0 < self.timeout_s <= 120:
            raise ValueError("timeout_s must be between 0 and 120")

    async def propose(self, scenario: Scenario, persona: Persona) -> MoveIntent:
        scenario = Scenario.model_validate(scenario)
        persona = Persona.model_validate(persona)
        request_id = "r" + uuid.uuid4().hex
        request = MoveDecisionRequest(
            request_id=request_id,
            scenario=scenario,
            persona=persona,
        )

        # Reserve before network I/O; uncertain outcomes are not automatically refunded.
        self.budget.consume("gemma")
        token = await self.token_source.token(self.audience)
        payload = await self.transport.post_json(
            self.endpoint.rstrip("/") + "/v1/move-intent",
            request.model_dump(mode="json"),
            headers={"Authorization": f"Bearer {token}"},
            timeout_s=self.timeout_s,
        )
        response = MoveDecisionResponse.model_validate_json(
            json.dumps(payload, ensure_ascii=False, allow_nan=False)
        )
        if response.request_id != request_id:
            raise ValueError("model service request_id mismatch")
        if response.intent.actor_id != persona.id:
            raise ValueError("model service cannot act as another persona")
        return response.intent
