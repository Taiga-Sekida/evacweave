"""Gemma inference service without a GPU: the runtime's guards and the HTTP boundary."""

import json
import types
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import apps.gemma_service.app as gemma_app
from apps.gemma_service.runtime import TransformersGemmaRuntime
from packages.domain.ai import MoveDecisionRequest, MoveDecisionResponse

ROOT = Path(__file__).resolve().parents[1]


def _request(scenario) -> MoveDecisionRequest:
    return MoveDecisionRequest(request_id="req1", scenario=scenario, persona=scenario.personas[0])


@pytest.fixture
def registry(tmp_path):
    path = tmp_path / "adapter_registry.json"
    path.write_text((ROOT / "models" / "adapter_registry.example.json").read_text(encoding="utf-8"))
    return str(path)


@pytest.fixture
def runtime_factory(monkeypatch, registry):
    """Build the runtime with the model stack replaced by a scripted text generator."""

    def build(generated: str) -> TransformersGemmaRuntime:
        def fake_load(self):
            self.model = types.SimpleNamespace(set_adapter=lambda name: None)
            self.processor = types.SimpleNamespace(
                tokenizer=types.SimpleNamespace(apply_chat_template=lambda messages, **kw: "PROMPT")
            )
            self.pipe = lambda **kwargs: [{"generated_text": generated}]

        monkeypatch.setattr(TransformersGemmaRuntime, "_load", fake_load)
        return TransformersGemmaRuntime(registry)

    return build


def _intent(actor: str) -> str:
    return json.dumps(
        {"action": "move", "actor_id": actor, "shelter_id": "shelter_b", "route": ["road_b1", "road_b2"]}
    )


def test_runtime_returns_a_strict_intent_with_model_identity(runtime_factory, scenario):
    runtime = runtime_factory(_intent("resident_01") + "<turn|>")
    response = runtime.decide_move(_request(scenario))
    assert response.intent.route == ("road_b1", "road_b2")
    assert response.model.adapter_id == "resident_v1"
    assert response.request_id == "req1"


def test_runtime_refuses_to_act_as_another_resident(runtime_factory, scenario):
    runtime = runtime_factory(_intent("resident_02"))
    with pytest.raises(ValueError, match="another persona"):
        runtime.decide_move(_request(scenario))


def test_runtime_rejects_free_text(runtime_factory, scenario):
    runtime = runtime_factory("Sure! Go to shelter B.")
    with pytest.raises(ValidationError):
        runtime.decide_move(_request(scenario))


def test_runtime_prompt_is_the_persona_view_only(scenario):
    messages = TransformersGemmaRuntime._prompt(_request(scenario))
    observation = json.loads(messages[1]["content"])
    assert observation["persona"]["id"] == "resident_01"
    assert set(observation) == {"persona", "deadline_s", "roads", "shelters"}


@pytest.mark.parametrize("kwargs", [{"role": "shelter"}, {"max_new_tokens": 16}, {"max_new_tokens": 4096}])
def test_runtime_rejects_unsupported_configuration(registry, kwargs):
    with pytest.raises(ValueError):
        TransformersGemmaRuntime(registry, **kwargs)


client = TestClient(gemma_app.app)


def test_service_serves_a_validated_decision(monkeypatch, scenario):
    class Runtime:
        def decide_move(self, request):
            return MoveDecisionResponse(
                request_id=request.request_id,
                model={"base_model": "/models/base", "adapter_id": "resident_v1"},
                intent=json.loads(_intent(request.persona.id)) | {"route": ("road_b1", "road_b2")},
                inference_ms=5,
            )

    monkeypatch.setattr(gemma_app, "_get_runtime", Runtime)
    response = client.post(
        "/v1/move-intent",
        content=_request(scenario).model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 200
    assert response.json()["intent"]["actor_id"] == "resident_01"


def test_service_maps_model_output_errors_to_422(monkeypatch, scenario):
    class Runtime:
        def decide_move(self, request):
            raise ValueError("resident adapter acted as another persona")

    monkeypatch.setattr(gemma_app, "_get_runtime", Runtime)
    response = client.post(
        "/v1/move-intent",
        content=_request(scenario).model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 422


def test_service_without_model_artifacts_is_unavailable(monkeypatch, scenario):
    monkeypatch.delenv("ADAPTER_REGISTRY_PATH", raising=False)
    monkeypatch.setattr(gemma_app, "_runtime", None)
    response = client.post(
        "/v1/move-intent",
        content=_request(scenario).model_dump_json(),
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 503


@pytest.mark.parametrize(("raw", "status"), [(b'{"request_id":"a","request_id":"b"}', 422), (b"{}", 422)])
def test_service_rejects_invalid_requests_before_inference(raw, status):
    response = client.post("/v1/move-intent", content=raw, headers={"content-type": "application/json"})
    assert response.status_code == status
    assert client.get("/health").json() == {"status": "ok"}
