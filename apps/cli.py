"""Local-only entry point.

Simulate:  exit 0=PASS, 1=FAIL, 2=unreadable or invalid input.
--agent:   run the guarded agent loop with the rule policy and print its trace;
           exit 0=verified outcome, 1=escalated/halted, 2=invalid input.
"""

import argparse
import asyncio
import json
import sys

from packages.agents.loop import RulePolicy, run_agent
from packages.domain.io import load_contract
from packages.domain.models import Plan, Scenario
from packages.simulation.engine import simulate


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--agent", action="store_true", help="run the rule-policy agent loop")
    args = parser.parse_args()
    try:
        scenario = load_contract(args.scenario, Scenario)
        plan = load_contract(args.plan, Plan)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        # Do not dump arbitrary input or filesystem content into an error log.
        print(json.dumps({"status": "ERROR", "error_type": type(error).__name__}), file=sys.stderr)
        return 2
    if args.agent:
        trace = asyncio.run(run_agent(scenario, plan, RulePolicy()))
        print(trace.model_dump_json(indent=2))
        return 0 if trace.outcome.startswith("verified") else 1
    result = simulate(scenario, plan)
    print(result.model_dump_json(indent=2))
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
