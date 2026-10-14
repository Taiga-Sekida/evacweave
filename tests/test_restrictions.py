"""Shelter restrictions: the Core rejects them and the repair search never proposes them."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from packages.domain.models import Plan, Scenario
from packages.repair.planner import propose_repair
from packages.simulation.engine import simulate

DISTRICT = Path(__file__).resolve().parents[1] / "data" / "demo_area" / "river_district"


def _district(restrictions=()):
    data = json.loads((DISTRICT / "scenario.json").read_text(encoding="utf-8"))
    data["restrictions"] = [
        dict(zip(("actor_id", "shelter_id", "reason"), r, strict=True)) for r in restrictions
    ]
    return Scenario.model_validate_json(json.dumps(data))


def _plan():
    return Plan.model_validate_json((DISTRICT / "plan.json").read_text(encoding="utf-8"))


def test_no_restrictions_serialise_and_simulate_exactly_as_before():
    plain = _district()
    assert "restrictions" not in plain.model_dump(mode="json")
    data = json.loads((DISTRICT / "scenario.json").read_text(encoding="utf-8"))
    assert simulate(plain, _plan()) == simulate(Scenario.model_validate_json(json.dumps(data)), _plan())


def test_a_resident_sent_to_a_restricted_shelter_is_rejected():
    plan = _plan()
    first = plan.intents[0]
    scenario = _district([(first.actor_id, first.shelter_id, "step_free_access")])
    result = simulate(scenario, plan)
    assert result.status == "FAIL"
    assert first.actor_id in result.not_evacuated
    rejected = [e for e in result.events if e.actor_id == first.actor_id and e.kind == "rejected"]
    assert [e.reason for e in rejected] == ["unsuitable_shelter"]
    assert "restrictions" in scenario.model_dump(mode="json")
    assert result.input_digest != simulate(_district(), plan).input_digest


def test_the_repair_search_never_proposes_a_restricted_shelter():
    scenario = _district(
        [("north_01", "school_shelter", "step_free_access"), ("east_01", "hall_shelter", "location_privacy")]
    )
    repaired = propose_repair(scenario, _plan())
    assert repaired is not None
    plan, result = repaired
    assert result.status == "PASS"
    chosen = {(i.actor_id, i.shelter_id) for i in plan.intents}
    assert ("north_01", "school_shelter") not in chosen
    assert ("east_01", "hall_shelter") not in chosen


def test_restrictions_that_leave_no_shelter_give_no_plan():
    every = [("north_01", s, "night_safety") for s in ("school_shelter", "hall_shelter", "hill_shelter")]
    assert propose_repair(_district(every), _plan()) is None


@pytest.mark.parametrize(
    "restrictions",
    [
        [("nobody", "school_shelter", "pet_companion")],
        [("north_01", "no_shelter", "pet_companion")],
        [("north_01", "school_shelter", "pet_companion"), ("north_01", "school_shelter", "infant_care")],
        [("north_01", "school_shelter", "because")],
    ],
)
def test_restrictions_must_name_known_residents_shelters_and_reasons(restrictions):
    with pytest.raises(ValidationError):
        _district(restrictions)
