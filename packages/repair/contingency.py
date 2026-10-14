"""Contingency planning: a verified fallback plan for each single point of failure.

For every road whose closure breaks the current plan, re-plan in the faulted world
and keep the fallback only if the Simulation Core passes it there.
"""

from __future__ import annotations

from packages.domain.agent import ContingencyPlan, FaultProbe
from packages.domain.models import Plan, Scenario
from packages.redteam.probes import with_road_closed
from packages.repair.planner import SearchBudgetExceeded, propose_repair


def plan_contingencies(
    scenario: Scenario, plan: Plan, probes: tuple[FaultProbe, ...]
) -> tuple[ContingencyPlan, ...]:
    plans: list[ContingencyPlan] = []
    failing = [probe for probe in probes if probe.status == "FAIL"]
    for number, probe in enumerate(failing, start=1):
        faulted = with_road_closed(scenario, probe.road_id)
        if faulted is None:
            plans.append(ContingencyPlan(road_id=probe.road_id, reason="no_verified_plan"))
            continue
        try:
            # Ordinal ids: a road id may already use the whole 64-character identifier.
            repaired = propose_repair(faulted, plan, plan_id=f"contingency_{number}")
        except SearchBudgetExceeded:
            plans.append(ContingencyPlan(road_id=probe.road_id, reason="search_budget_exhausted"))
            continue
        if repaired is None:
            plans.append(ContingencyPlan(road_id=probe.road_id, reason="no_verified_plan"))
            continue
        fallback, result = repaired
        plans.append(
            ContingencyPlan(
                road_id=probe.road_id,
                reason="verified_alternative",
                plan=fallback,
                input_digest=result.input_digest,
                last_arrival_s=max(e.time_s for e in result.events if e.kind == "arrived"),
            )
        )
    return tuple(plans)
