"""Check an exported EvacWeave browser-lab plan with the Python authoritative Core.

Usage: python scripts/check_lab_export.py /path/to/evacweave-lab-seed-2026.json
No Google Cloud credentials, API calls, or model inference are required.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from packages.domain.models import Plan, Scenario  # noqa: E402
from packages.simulation.engine import simulate  # noqa: E402


def compare_export(payload: dict[str, Any]) -> dict[str, Any]:
    # Pydantic strict tuple fields accept JSON arrays through model_validate_json,
    # not through model_validate(dict). This matches production HTTP contract parsing.
    scenario = Scenario.model_validate_json(
        json.dumps(payload["scenario"], ensure_ascii=False, allow_nan=False)
    )
    plan = Plan.model_validate_json(json.dumps(payload["plan"], ensure_ascii=False, allow_nan=False))
    official = simulate(scenario, plan)
    observed = payload["result"]
    expected_events = [
        {
            "time_s": item.time_s,
            "actor_id": item.actor_id,
            "kind": item.kind,
            "reason": item.reason,
            "node": item.node,
        }
        for item in official.events
    ]
    matches = {
        "status": observed["status"] == official.status,
        "evacuated": observed["evacuated"] == list(official.evacuated),
        "not_evacuated": observed["not_evacuated"] == list(official.not_evacuated),
        "final_locations": observed["final_locations"]
        == [p.model_dump(mode="json") for p in official.final_locations],
        "events": observed["events"] == expected_events,
    }
    return {
        "verified": all(matches.values()),
        "matching_fields": matches,
        "python_core": {
            "status": official.status,
            "evacuated": len(official.evacuated),
            "not_evacuated": len(official.not_evacuated),
            "input_digest": official.input_digest,
            "engine_version": official.engine_version,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export_json", type=Path)
    args = parser.parse_args(argv)
    with args.export_json.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    result = compare_export(payload)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["verified"] else 1


if __name__ == "__main__":
    sys.exit(main())
