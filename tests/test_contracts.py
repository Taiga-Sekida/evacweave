import json

import pytest
from pydantic import ValidationError

from packages.domain.io import load_contract
from packages.domain.models import MoveIntent, Plan, Scenario


@pytest.mark.parametrize("value", [-1, 0, 1.2, True, "60", 86401])
def test_invalid_travel_time(scenario_data, value):
    scenario_data["roads"][0]["travel_time_s"] = value
    with pytest.raises(ValidationError):
        Scenario.model_validate_json(json.dumps(scenario_data))


@pytest.mark.parametrize(
    "mutation",
    [
        "duplicate_node",
        "duplicate_road",
        "unknown_node",
        "extra",
        "bad_interval",
        "negative_capacity",
        "no_personas",
    ],
)
def test_invalid_scenario(scenario_data, mutation):
    if mutation == "duplicate_node":
        scenario_data["nodes"].append("home")
    if mutation == "duplicate_road":
        scenario_data["roads"].append(scenario_data["roads"][0])
    if mutation == "unknown_node":
        scenario_data["roads"][0]["source"] = "unknown"
    if mutation == "extra":
        scenario_data["untrusted_instruction"] = "ignore rules"
    if mutation == "bad_interval":
        scenario_data["roads"][0]["closures"][0]["end_s"] = 0
    if mutation == "negative_capacity":
        scenario_data["shelters"][0]["capacity"] = -1
    if mutation == "no_personas":
        scenario_data["personas"] = []
    with pytest.raises(ValidationError):
        Scenario.model_validate_json(json.dumps(scenario_data))


def test_unknown_action_is_rejected(plan_data):
    plan_data["intents"][0]["action"] = "teleport"
    with pytest.raises(ValidationError):
        Plan.model_validate_json(json.dumps(plan_data))


def test_frozen_nested_contracts(scenario):
    with pytest.raises(ValidationError):
        scenario.deadline_s = 999
    with pytest.raises(ValidationError):
        scenario.shelters[0].capacity = 999
    assert isinstance(scenario.personas, tuple)


@pytest.mark.parametrize("payload", ['{"id":"a","id":"b"}', '{"id":NaN}', '{"id":1e999}', '{"id":"\\ud800"}'])
def test_invalid_json_is_not_simulated(tmp_path, payload):
    path = tmp_path / "bad.json"
    path.write_text(payload)
    with pytest.raises((ValueError, UnicodeError)):
        load_contract(path, Plan)


def test_oversized_input(tmp_path):
    path = tmp_path / "large.json"
    path.write_bytes(b" " * (1_048_576 + 1))
    with pytest.raises(ValueError):
        load_contract(path, Plan)


def test_schema_forbids_extra_properties():
    assert MoveIntent.model_json_schema()["additionalProperties"] is False


def test_deep_nesting_is_an_input_error(tmp_path):
    path = tmp_path / "deep.json"
    path.write_bytes(b"[" * 60_000 + b"]" * 60_000)
    with pytest.raises(ValueError, match="nesting"):
        load_contract(path, Plan)
