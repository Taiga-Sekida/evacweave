"""Not deployed (roadmap): a private Cloud Run GPU inference service for fine-tuned Gemma adapters.

No script deploys it and neither running service calls it; infra/cloud-run/gemma-gpu.yaml
is the intended deployment shape.
"""

from __future__ import annotations

import os
from threading import Lock

from fastapi import FastAPI, HTTPException, Request
from pydantic import ValidationError

from apps.gemma_service.runtime import TransformersGemmaRuntime
from apps.http import read_contract
from packages.domain.ai import MoveDecisionRequest, MoveDecisionResponse

app = FastAPI(title="Disaster Gemma Runtime", version="0.1.0")
_runtime: TransformersGemmaRuntime | None = None
_runtime_lock = Lock()
MAX_REQUEST_BYTES = 262_144


def _get_runtime() -> TransformersGemmaRuntime:
    global _runtime
    if _runtime is None:
        with _runtime_lock:
            if _runtime is None:
                registry = os.environ.get("ADAPTER_REGISTRY_PATH")
                if not registry:
                    raise RuntimeError("ADAPTER_REGISTRY_PATH is required")
                max_tokens = int(os.environ.get("GEMMA_MAX_NEW_TOKENS", "256"))
                role = os.environ.get("GEMMA_ROLE", "resident")
                _runtime = TransformersGemmaRuntime(registry, role=role, max_new_tokens=max_tokens)
    return _runtime


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/move-intent", response_model=MoveDecisionResponse)
async def move_intent(http_request: Request) -> MoveDecisionResponse:
    request = await read_contract(http_request, MoveDecisionRequest, max_bytes=MAX_REQUEST_BYTES)
    try:
        return _get_runtime().decide_move(request)
    except (ValidationError, ValueError, KeyError) as exc:
        raise HTTPException(
            status_code=422, detail="request or model output failed strict validation"
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail="model runtime unavailable") from exc
