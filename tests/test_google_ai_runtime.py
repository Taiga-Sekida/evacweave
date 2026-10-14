import asyncio
import json
import sys
import types

import pytest
from pydantic import ValidationError

from packages.agents.gemma_provider import CloudRunGemmaDecisionProvider
from packages.agents.loop import PolicyOutputError
from packages.agents.orchestrator import (
    REQUEST_PROFILES,
    GeminiAgentPolicy,
    GeminiChoice,
    GoogleGenAITransport,
    observation_prompt,
)
from packages.domain.agent import AgentObservation
from packages.domain.ai import AdapterRegistry, MoveDecisionRequest
from packages.domain.models import Persona, Scenario
from packages.services.budget import AttemptBudget, BudgetExceeded


def scenario() -> Scenario:
    return Scenario.model_validate_json(
        json.dumps(
            {
                "id": "ScenarioA",
                "dataset_version": "DemoV1",
                "synthetic": True,
                "deadline_s": 600,
                "nodes": ["Home", "Crossing", "ShelterNode"],
                "roads": [
                    {
                        "id": "RoadA",
                        "source": "Home",
                        "target": "Crossing",
                        "travel_time_s": 60,
                        "closures": [],
                    },
                    {
                        "id": "RoadB",
                        "source": "Crossing",
                        "target": "ShelterNode",
                        "travel_time_s": 60,
                        "closures": [],
                    },
                ],
                "shelters": [{"id": "ShelterA", "node": "ShelterNode", "capacity": 10, "opens_at_s": 0}],
                "personas": [{"id": "ResidentA", "start_node": "Home"}],
            }
        )
    )


def test_adapter_registry_rejects_duplicate_role():
    with pytest.raises(ValidationError):
        AdapterRegistry.model_validate_json(
            json.dumps(
                {
                    "base_model": "/models/base",
                    "processor_model": "/models/processor",
                    "adapters": [
                        {"role": "resident", "adapter_id": "A", "path": "/a"},
                        {"role": "resident", "adapter_id": "B", "path": "/b"},
                    ],
                }
            )
        )


def test_move_request_requires_persona_in_scenario():
    with pytest.raises(ValidationError):
        MoveDecisionRequest(
            request_id="req1",
            scenario=scenario(),
            persona=Persona(id="Other", start_node="Home"),
        )


def test_attempt_budget_is_fail_closed():
    budget = AttemptBudget(gemma_limit=1, gemini_limit=1)
    budget.consume("gemma")
    with pytest.raises(BudgetExceeded):
        budget.consume("gemma")
    assert budget.snapshot()["gemma_used"] == 1


class FakeTokenSource:
    async def token(self, audience: str) -> str:
        assert audience == "https://gemma.example"
        return "token"


class FakeJsonTransport:
    def __init__(self, wrong_actor: bool = False, fail: bool = False):
        self.wrong_actor = wrong_actor
        self.fail = fail
        self.calls = []

    async def post_json(self, url, payload, *, headers, timeout_s):
        self.calls.append((url, payload, headers, timeout_s))
        if self.fail:
            raise OSError("network outcome unknown")
        actor = "Other" if self.wrong_actor else payload["persona"]["id"]
        return {
            "schema_version": "move-decision-response/v1",
            "request_id": payload["request_id"],
            "role": "resident",
            "model": {
                "provider": "fine-tuned-gemma",
                "base_model": "/models/base",
                "adapter_id": "resident_v1",
            },
            "intent": {
                "action": "move",
                "actor_id": actor,
                "shelter_id": "ShelterA",
                "departure_s": 0,
                "route": ["RoadA", "RoadB"],
            },
            "inference_ms": 12,
        }


def test_cloud_run_provider_rejects_identity_token_origin_mismatch():
    with pytest.raises(ValueError, match="must match"):
        CloudRunGemmaDecisionProvider(
            endpoint="https://evil.example",
            audience="https://gemma.example",
            transport=FakeJsonTransport(),
            token_source=FakeTokenSource(),
            budget=AttemptBudget(gemma_limit=1, gemini_limit=0),
        )


def test_cloud_run_provider_returns_strict_intent_and_sends_identity_token():
    transport = FakeJsonTransport()
    budget = AttemptBudget(gemma_limit=2, gemini_limit=0)
    provider = CloudRunGemmaDecisionProvider(
        endpoint="https://gemma.example",
        audience="https://gemma.example",
        transport=transport,
        token_source=FakeTokenSource(),
        budget=budget,
    )
    intent = asyncio.run(provider.propose(scenario(), scenario().personas[0]))
    assert intent.actor_id == "ResidentA"
    assert transport.calls[0][2]["Authorization"] == "Bearer token"
    assert budget.snapshot()["gemma_used"] == 1


def test_cloud_run_provider_rejects_cross_actor_output():
    provider = CloudRunGemmaDecisionProvider(
        endpoint="https://gemma.example",
        audience="https://gemma.example",
        transport=FakeJsonTransport(wrong_actor=True),
        token_source=FakeTokenSource(),
        budget=AttemptBudget(gemma_limit=1, gemini_limit=0),
    )
    with pytest.raises(ValueError, match="another persona"):
        asyncio.run(provider.propose(scenario(), scenario().personas[0]))


def test_failed_transport_still_consumes_attempt():
    budget = AttemptBudget(gemma_limit=1, gemini_limit=0)
    provider = CloudRunGemmaDecisionProvider(
        endpoint="https://gemma.example",
        audience="https://gemma.example",
        transport=FakeJsonTransport(fail=True),
        token_source=FakeTokenSource(),
        budget=budget,
    )
    with pytest.raises(OSError):
        asyncio.run(provider.propose(scenario(), scenario().personas[0]))
    assert budget.snapshot()["gemma_used"] == 1


def observation(**overrides) -> AgentObservation:
    fields = {
        "step": 0,
        "steps_remaining": 6,
        "plan_id": "PlanA",
        "plan_status": "FAIL",
        "residents": 1,
        "evacuated": 0,
        "rejection_reasons": ("route_blocked",),
        "repair_attempted": False,
        "stress_tested": False,
        "single_points_of_failure": 0,
        "contingencies_planned": False,
        "contingencies_covered": 0,
        "allowed_actions": ("run_repair", "escalate"),
    }
    fields.update(overrides)
    return AgentObservation(**fields)


class FakeStructuredTransport:
    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.calls = []

    async def generate(self, **kwargs):
        self.calls.append(kwargs)
        assert kwargs["response_schema"] is GeminiChoice
        return self.payloads.pop(0)


def gemini(transport, limit=2) -> GeminiAgentPolicy:
    return GeminiAgentPolicy("gemini-test", transport, AttemptBudget(gemma_limit=0, gemini_limit=limit))


def test_gemini_policy_returns_strict_decision_and_counts_calls():
    transport = FakeStructuredTransport({"action": "run_repair", "reason_code": "plan_failed_verification"})
    policy = gemini(transport)
    decision = asyncio.run(policy.decide(observation()))
    assert (decision.action, decision.reason_code) == ("run_repair", "plan_failed_verification")
    assert policy.model_calls == 1
    assert transport.calls[0]["max_output_tokens"] == 512


def test_gemini_prompt_is_data_with_the_allowed_menu_only():
    sent = json.loads(observation_prompt(observation()))
    assert sent["allowed_decisions"] == [
        {"action": "run_repair", "reason_codes": ["plan_failed_verification"]},
        {"action": "escalate", "reason_codes": ["no_verified_plan", "needs_human_judgement"]},
    ]
    assert sent["observation"]["plan_status"] == "FAIL"
    # The caller-chosen plan id is the one free-form field; it never reaches the model.
    assert "plan_id" not in sent["observation"]
    assert "PlanA" not in observation_prompt(observation())


@pytest.mark.parametrize(
    "payload",
    [
        {"action": "run_repair", "reason_code": "verified_plan_ready"},  # reason does not fit action
        {"action": "teleport", "reason_code": "plan_failed_verification"},
        {"action": "run_repair", "reason_code": "plan_failed_verification", "verdict": "PASS"},
    ],
)
def test_gemini_policy_rejects_malformed_output(payload):
    with pytest.raises(PolicyOutputError):
        asyncio.run(gemini(FakeStructuredTransport(payload)).decide(observation()))


def test_gemini_budget_prevents_extra_call():
    transport = FakeStructuredTransport({"action": "run_repair", "reason_code": "plan_failed_verification"})
    policy = gemini(transport, limit=1)
    asyncio.run(policy.decide(observation()))
    with pytest.raises(BudgetExceeded):
        asyncio.run(policy.decide(observation()))
    assert len(transport.calls) == 1


def test_gemini_call_timeout_is_enforced():
    class Slow:
        async def generate(self, **kwargs):
            await asyncio.sleep(1)

    policy = GeminiAgentPolicy("gemini-test", Slow(), AttemptBudget(0, 1), timeout_s=0.01)
    with pytest.raises(TimeoutError):
        asyncio.run(policy.decide(observation()))
    assert policy.model_calls == 1  # an uncertain attempt is never refunded


def test_gemini_choice_schema_is_enums_only():
    schema = GeminiChoice.model_json_schema()
    assert set(schema["properties"]) == {"action", "reason_code"}
    assert "additionalProperties" not in schema
    assert all("enum" in prop for prop in schema["properties"].values())
    # The class docstring is sent to the model as the schema description: keep it model-facing.
    assert schema["description"].startswith("One decision")
    assert len(schema["description"]) < 120


def _fake_genai_modules(monkeypatch):
    """Stand-in for google.genai so the request shape is checked without network or SDK."""

    def config_type(**kwargs):
        return types.SimpleNamespace(**kwargs)

    fake_types = types.SimpleNamespace(
        GenerateContentConfig=config_type,
        ThinkingConfig=config_type,
        ThinkingLevel=types.SimpleNamespace(LOW="LOW", MINIMAL="MINIMAL"),
        HttpOptions=config_type,
        HttpRetryOptions=config_type,
    )
    genai = types.ModuleType("google.genai")
    genai.types = fake_types
    google = types.ModuleType("google")
    google.genai = genai
    monkeypatch.setitem(sys.modules, "google", google)
    monkeypatch.setitem(sys.modules, "google.genai", genai)


class _FakeModels:
    def __init__(self, text):
        self.text = text
        self.kwargs = None

    async def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(parsed=None, text=self.text)


def _transport(models, profile="default") -> GoogleGenAITransport:
    return GoogleGenAITransport(
        project="p",
        profile=REQUEST_PROFILES[profile],
        _client=types.SimpleNamespace(aio=types.SimpleNamespace(models=models)),
    )


def test_genai_transport_sends_thinking_level_not_budget(monkeypatch):
    _fake_genai_modules(monkeypatch)
    models = _FakeModels(json.dumps({"action": "run_repair", "reason_code": "plan_failed_verification"}))
    payload = asyncio.run(
        _transport(models).generate(
            model="gemini-3.5-flash",
            system_instruction="sys",
            prompt="{}",
            response_schema=GeminiChoice,
            max_output_tokens=512,
            timeout_s=12.5,
        )
    )
    config = models.kwargs["config"]
    assert models.kwargs["model"] == "gemini-3.5-flash"
    assert config.thinking_config.thinking_level == "LOW"
    assert not hasattr(config.thinking_config, "thinking_budget")
    assert config.max_output_tokens == 512
    assert config.response_schema is GeminiChoice
    # Gemini 3: the temperature is left at the model's default (1.0), as Google recommends.
    assert not hasattr(config, "temperature")
    # The SDK gives up at the caller's limit and makes exactly one attempt (no unreserved retry).
    assert config.http_options.timeout == 12_500
    assert config.http_options.retry_options.attempts == 1
    assert payload == {"action": "run_repair", "reason_code": "plan_failed_verification"}


@pytest.mark.parametrize("text", ["", "[1]"])
def test_genai_transport_rejects_non_object_output(monkeypatch, text):
    _fake_genai_modules(monkeypatch)
    with pytest.raises(PolicyOutputError):
        asyncio.run(
            _transport(_FakeModels(text)).generate(
                model="m",
                system_instruction="s",
                prompt="{}",
                response_schema=GeminiChoice,
                max_output_tokens=64,
                timeout_s=5.0,
            )
        )


def _ask(transport):
    return asyncio.run(
        transport.generate(
            model="gemini-3.5-flash",
            system_instruction="sys",
            prompt="{}",
            response_schema=GeminiChoice,
            max_output_tokens=512,
            timeout_s=10.0,
        )
    )


def test_minimal_profile_thinks_minimally_with_the_schema(monkeypatch):
    _fake_genai_modules(monkeypatch)
    models = _FakeModels(json.dumps({"action": "escalate", "reason_code": "no_verified_plan"}))
    _ask(_transport(models, "minimal"))
    config = models.kwargs["config"]
    assert config.thinking_config.thinking_level == "MINIMAL"
    assert config.response_schema is GeminiChoice and config.system_instruction == "sys"


def test_json_profile_states_the_schema_instead_of_constraining_it(monkeypatch):
    _fake_genai_modules(monkeypatch)
    models = _FakeModels(json.dumps({"action": "escalate", "reason_code": "no_verified_plan"}))
    assert _ask(_transport(models, "json")) == {"action": "escalate", "reason_code": "no_verified_plan"}
    config = models.kwargs["config"]
    assert not hasattr(config, "response_schema")
    assert config.response_mime_type == "application/json"
    assert config.system_instruction.startswith("sys\n")
    assert '"reason_code"' in config.system_instruction and "JSON Schema" in config.system_instruction


def test_an_answer_cut_off_at_the_token_limit_is_reported_as_such(monkeypatch):
    _fake_genai_modules(monkeypatch)

    class Truncated(_FakeModels):
        async def generate_content(self, **kwargs):
            self.kwargs = kwargs
            candidate = types.SimpleNamespace(finish_reason=types.SimpleNamespace(name="MAX_TOKENS"))
            return types.SimpleNamespace(
                parsed=None, text='{"restrictions": [{"actor_id": "nor', candidates=[candidate]
            )

    from packages.agents.orchestrator import OutputTruncated

    with pytest.raises(OutputTruncated):
        asyncio.run(
            _transport(Truncated("")).generate(
                model="gemini-3.5-flash",
                system_instruction="sys",
                prompt="{}",
                response_schema=GeminiChoice,
                max_output_tokens=64,
                timeout_s=5,
            )
        )
