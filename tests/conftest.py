import json
from pathlib import Path

import pytest

from packages.domain.models import Plan, Scenario

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data/demo_area/blocked_route"


@pytest.fixture
def scenario_data():
    return json.loads((FIXTURE / "scenario.json").read_text())


@pytest.fixture
def plan_data():
    return json.loads((FIXTURE / "plan.json").read_text())


@pytest.fixture
def scenario(scenario_data):
    return Scenario.model_validate_json(json.dumps(scenario_data))


@pytest.fixture
def plan(plan_data):
    return Plan.model_validate_json(json.dumps(plan_data))


@pytest.fixture
def repaired():
    return Plan.model_validate_json((FIXTURE / "repaired_plan.json").read_text())
