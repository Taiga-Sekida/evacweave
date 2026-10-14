import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from packages.agents.provider import FixtureDecisionProvider, evaluate_provider
from packages.domain.models import MoveIntent, Plan

ROOT = Path(__file__).resolve().parents[1]


def test_fixture_provider(scenario, repaired):
    result = asyncio.run(evaluate_provider(scenario, FixtureDecisionProvider(repaired)))
    assert result.status == "PASS"


def test_provider_rejects_actor_spoofing(scenario):
    class Spoof:
        async def propose(self, scenario, persona):
            return MoveIntent(actor_id="another_actor", shelter_id="shelter_b", route=("road_b1", "road_b2"))

    with pytest.raises(ValueError, match="another persona"):
        asyncio.run(evaluate_provider(scenario, Spoof()))


def test_provider_failure_is_error_not_scenario_failure(scenario):
    class Broken:
        async def propose(self, scenario, persona):
            raise RuntimeError("provider unavailable")

    with pytest.raises(RuntimeError):
        asyncio.run(evaluate_provider(scenario, Broken()))


def test_missing_fixture(scenario):
    with pytest.raises(ValueError):
        asyncio.run(evaluate_provider(scenario, FixtureDecisionProvider(Plan(id="empty", intents=()))))


@pytest.mark.parametrize(
    ("plan", "exit_code", "status"), [("plan.json", 1, "FAIL"), ("repaired_plan.json", 0, "PASS")]
)
def test_cli(plan, exit_code, status):
    folder = ROOT / "data/demo_area/blocked_route"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.cli",
            "--scenario",
            str(folder / "scenario.json"),
            "--plan",
            str(folder / plan),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == exit_code
    assert json.loads(result.stdout)["status"] == status


def test_cli_input_error(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{}")
    result = subprocess.run(
        [sys.executable, "-m", "apps.cli", "--scenario", str(path), "--plan", str(path)],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert json.loads(result.stderr)["status"] == "ERROR"


def test_core_import_boundary():
    allowed = {"__future__", "hashlib", "heapq", "json", "packages.domain.models"}
    for path in (ROOT / "packages/simulation").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                assert all(alias.name in allowed for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert node.module in allowed


def test_domain_import_boundary():
    for path in (ROOT / "packages/domain").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert not (node.module or "").startswith(
                    ("packages.agents", "packages.simulation", "packages.services")
                )


@pytest.mark.parametrize(("plan", "exit_code", "outcome"), [("plan.json", 0, "verified_with_contingencies")])
def test_cli_agent(plan, exit_code, outcome):
    folder = ROOT / "data/demo_area/blocked_route"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "apps.cli",
            "--agent",
            "--scenario",
            str(folder / "scenario.json"),
            "--plan",
            str(folder / plan),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == exit_code
    trace = json.loads(result.stdout)
    assert trace["outcome"] == outcome and trace["policy"] == "rule-policy"


def test_record_script_refuses_without_paid_call_confirmation():
    result = subprocess.run(
        [
            sys.executable,
            "scripts/record_gemini_run.py",
            "--service-url",
            "https://disaster-agentic-orchestrator-x.a.run.app",
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "--confirm-paid-call" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "http://disaster-agentic-orchestrator-x.a.run.app",
        "https://example.com",
        "https://disaster-agentic-orchestrator-x.a.run.app/path",
        "https://evil.a.run.app",  # the identity token only goes to the agent service
    ],
)
def test_record_script_only_targets_cloud_run_origins(url):
    result = subprocess.run(
        [sys.executable, "scripts/record_gemini_run.py", "--service-url", url, "--confirm-paid-call"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2
    assert "run.app" in result.stderr


@pytest.mark.parametrize(
    ("args", "code", "key", "value"),
    [
        (["--plan", "plan.json"], 1, "status", "FAIL"),
        (["--plan", "repaired_plan.json"], 0, "status", "PASS"),
        (["--plan", "plan.json", "--agent"], 0, "outcome", "verified_with_contingencies"),
    ],
)
def test_cli_in_process(monkeypatch, capsys, args, code, key, value):
    from apps import cli

    folder = ROOT / "data/demo_area/blocked_route"
    argv = ["cli", "--scenario", str(folder / "scenario.json")]
    argv += [str(folder / a) if a.endswith(".json") else a for a in args]
    monkeypatch.setattr(sys, "argv", argv)
    assert cli.main() == code
    assert json.loads(capsys.readouterr().out)[key] == value


def test_cli_in_process_reports_input_errors_without_echoing(monkeypatch, capsys, tmp_path):
    from apps import cli

    bad = tmp_path / "bad.json"
    bad.write_text('{"secret": "do not echo"}')
    monkeypatch.setattr(sys, "argv", ["cli", "--scenario", str(bad), "--plan", str(bad)])
    assert cli.main() == 2
    err = capsys.readouterr().err
    assert json.loads(err)["status"] == "ERROR" and "do not echo" not in err


def test_record_script_never_follows_redirects():
    import importlib.util

    spec = importlib.util.spec_from_file_location("record_gemini_run", ROOT / "scripts/record_gemini_run.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # urllib would resend the Authorization header to the redirect target.
    assert module._NoRedirect().redirect_request() is None
    assert any(isinstance(h, module._NoRedirect) for h in module._OPENER.handlers)
