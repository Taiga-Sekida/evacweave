"""The resident pool and the sweep: other groups of residents in the same homes.

No model is called anywhere here: the pool is reviewed data, a pattern is chosen by a
hash, and the sweep is the planner and the Simulation Core.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from apps.api.main import app
from packages.domain.care import (
    PATTERNS,
    CareSet,
    CareSweep,
    ResidentPool,
    care_pattern,
    one_per_pair,
    pattern_avoids,
    pattern_members,
)
from packages.domain.io import load_contract
from packages.domain.models import Plan, Scenario, ShelterRestriction
from packages.repair.sweep import _has_verified_plan, _world
from tests.test_care import live_care  # noqa: F401 - the fixture is used by name

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / "data" / "demo_area" / "river_district"
CARE = load_contract(FOLDER / "care.json", CareSet)
POOL = load_contract(FOLDER / "residents.json", ResidentPool)
SCENARIO = load_contract(FOLDER / "scenario.json", Scenario)
PLAN = load_contract(FOLDER / "plan.json", Plan)
client = TestClient(app)

SCHOOL, HALL, HILL = "school_shelter", "hall_shelter", "hill_shelter"
# Which shelters fail each need, read from the three facility texts in care.json.
FAILS = {
    "infant_care": {SCHOOL, HILL},
    "medical_power": {HALL, HILL},
    "night_safety": {HILL},
    "pet_companion": {SCHOOL, HALL},
    "language_support": {HALL, HILL},
    "dietary_needs": {HALL, HILL},
    "quiet_room": {SCHOOL, HILL},
}


def test_the_pool_is_one_hundred_fictional_residents_with_no_attribute_to_guess_from():
    raw = json.loads((FOLDER / "residents.json").read_text(encoding="utf-8"))
    assert raw["synthetic"] is True and len(POOL.residents) == 100
    # Name, age and the resident's own words: no origin, nationality, religion or race.
    assert all(set(resident) == {"id", "label", "note", "avoid"} for resident in raw["residents"])
    assert len({r.label for r in POOL.residents}) == 100


def test_the_first_twelve_are_the_scenarios_own_residents():
    first = care_pattern(CARE, POOL, 0)
    assert first.residents == CARE.residents and first.shelters == CARE.shelters
    assert set(first.reviewed_restrictions) == set(CARE.reviewed_restrictions)


def test_every_reviewed_restriction_follows_the_facilities():
    for resident in POOL.residents:
        reasons: dict[str, set[str]] = {}
        for avoid in resident.avoid:
            reasons.setdefault(avoid.reason, set()).add(avoid.shelter_id)
        for reason, shelters in reasons.items():
            if reason == "location_privacy":
                assert len(shelters) == 1, resident.label  # the one place the note names
            elif reason == "step_free_access":
                # Steps at the school, a slope and gravel at the hill: a resident who
                # manages a step avoids only the hill.
                assert shelters in ({HILL}, {SCHOOL, HILL}), resident.label
            else:
                assert shelters == FAILS[reason], resident.label


@pytest.mark.parametrize(
    "label",
    [
        "グエンさん（27）",
        "シルバさん（44）",
        "王さん（39）",
        "アフメドさん（31）",
        "スミスさん（35）",
        "松本さん（24）",
    ],
)
def test_a_resident_from_abroad_whose_note_states_no_need_has_no_restriction(label):
    (resident,) = [r for r in POOL.residents if r.label == label]
    assert resident.avoid == ()


def test_patterns_are_fixed_draws_of_twelve_different_residents():
    assert pattern_members("river_district", 100, 12, 0) == tuple(range(12))
    draws = [pattern_members("river_district", 100, 12, k) for k in range(1, PATTERNS)]
    assert all(len(set(draw)) == 12 and all(0 <= i < 100 for i in draw) for draw in draws)
    assert len(set(draws)) == PATTERNS - 1
    assert draws[0] == pattern_members("river_district", 100, 12, 1)  # the same on every call
    with pytest.raises(ValueError):
        pattern_members("river_district", 10, 12, 1)


def test_a_pattern_keeps_the_homes_and_brings_the_pools_notes_and_restrictions():
    care = care_pattern(CARE, POOL, 4)
    assert [r.actor_id for r in care.residents] == [r.actor_id for r in CARE.residents]
    members = pattern_members("river_district", 100, 12, 4)
    assert [r.note for r in care.residents] == [POOL.residents[i].note for i in members]
    assert care.reviewed_restrictions == one_per_pair(pattern_avoids(CARE, POOL, 4))
    assert (care.pattern, care.patterns) == (4, PATTERNS)


def test_one_per_pair_keeps_the_first_reason_and_meeting_one_need_keeps_the_other():
    both = (
        ShelterRestriction(actor_id="north_01", shelter_id=HILL, reason="step_free_access"),
        ShelterRestriction(actor_id="north_01", shelter_id=HILL, reason="medical_power"),
    )
    (kept,) = one_per_pair(both)
    assert kept.reason == "step_free_access"
    world = _world(SCENARIO, both, [(HILL, "meet_need", "step_free_access")])
    assert [(r.shelter_id, r.reason) for r in world.restrictions] == [(HILL, "medical_power")]
    bigger = _world(SCENARIO, (), [(HALL, "add_capacity", None)])
    assert {s.id: s.capacity for s in bigger.shelters}[HALL] == 4 + 2


@pytest.fixture(scope="module")
def swept() -> CareSweep:
    return CareSweep.model_validate_json(client.get("/v1/agent/care/sweep").text)


def test_the_sweep_counts_the_groups_a_plan_can_serve(swept):
    assert len(swept.patterns) == PATTERNS
    assert swept.verified == sum(p.verified for p in swept.patterns)
    assert swept.patterns[0].verified  # the scenario's own residents
    assert {p.cause for p in swept.patterns if not p.verified} <= {"no_shelter", "capacity", "route"}
    # Each step of the sequence serves more groups than the one before.
    counts = [swept.verified, *(step.verified for step in swept.steps)]
    assert counts == sorted(set(counts)) and len(swept.steps) >= 1


def test_the_sweep_agrees_with_the_agent_on_every_group(swept):
    for check in swept.patterns:
        care = client.get(f"/v1/agent/care?pattern={check.pattern}").json()
        run = client.post(
            "/v1/agent/challenge",
            json={"scenario": "river_district", "restrictions": care["reviewed_restrictions"]},
        ).json()
        repair = next(s for s in run["trace"]["steps"] if s["decision"]["action"] == "run_repair")
        assert (repair["effect"]["plan_status"] == "PASS") == check.verified, check.pattern


def test_a_group_without_a_shelter_for_someone_names_them(swept):
    stranded = [p for p in swept.patterns if p.cause == "no_shelter"]
    assert stranded
    for check in stranded:
        care = care_pattern(CARE, POOL, check.pattern)
        world = Scenario.model_validate(
            SCENARIO.model_copy(update={"restrictions": care.reviewed_restrictions})
        )
        barred = {
            actor: {r.shelter_id for r in world.restrictions if r.actor_id == actor}
            for actor in check.residents
        }
        assert all(shelters == {SCHOOL, HALL, HILL} for shelters in barred.values())


def test_every_fix_really_rescues_the_groups_it_names(swept):
    for fix in swept.fixes:
        change = [(fix.shelter_id, fix.change, fix.reason)]
        for index in fix.rescued:
            avoids = pattern_avoids(CARE, POOL, index)
            assert not swept.patterns[index].verified and not _has_verified_plan(
                _world(SCENARIO, avoids), PLAN
            )
            assert _has_verified_plan(_world(SCENARIO, avoids, change), PLAN)


def test_patterns_out_of_range_or_without_a_pool_are_refused():
    assert client.get("/v1/agent/care?pattern=30").status_code == 422
    assert client.get("/v1/agent/care?pattern=-1").status_code == 422
    assert client.get("/v1/agent/care?scenario=base&pattern=1").status_code == 404
    assert client.get("/v1/agent/care/sweep?scenario=base").status_code == 404


def test_gemini_reads_the_group_the_visitor_is_looking_at(live_care):  # noqa: F811
    response = client.post("/v1/agent/care", json={"scenario": "river_district", "pattern": 4})
    assert response.status_code == 200, response.text
    (request,) = live_care
    assert [r.note for r in request.residents] == [r.note for r in care_pattern(CARE, POOL, 4).residents]
    assert (
        client.post("/v1/agent/care", json={"scenario": "river_district", "pattern": 30}).status_code == 422
    )
