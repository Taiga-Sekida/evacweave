"""Generate the shared contracts. Run from any directory."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from packages.domain.models import MoveIntent, Plan, Scenario, SimulationResult  # noqa: E402

if __name__ == "__main__":
    target = ROOT / "artifacts" / "schemas"
    target.mkdir(parents=True, exist_ok=True)
    for model in (Scenario, Plan, MoveIntent, SimulationResult):
        (target / f"{model.__name__}.json").write_text(
            json.dumps(model.model_json_schema(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print("Generated 4 schemas under artifacts/schemas")
