"""Scripts that make the paid Gemini runs: they refuse, verify and record correctly.

No network and no model call: HTTP and gcloud are faked in-process.
"""

import argparse
import asyncio
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest

from packages.agents.loop import RulePolicy, run_agent
from packages.domain.agent import RecordedAgentRun
from packages.domain.io import load_contract
from packages.domain.live import LiveRun, LiveStatus
from packages.domain.models import Plan, Scenario
from packages.services import gemini_attempts

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "data" / "demo_area" / "blocked_route"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _ScriptedGemini:
    """Test double: rule decisions labelled as a Gemini trace, to exercise the checks only."""

    name, model, prompt_version = "vertex-gemini", "gemini-3.5-flash", "orchestrator/v2"

    def __init__(self, fail: bool = False) -> None:
        self.model_calls = 0
        self.fail = fail

    async def decide(self, observation):
        self.model_calls += 1
        if self.fail:
            raise RuntimeError("PermissionDenied")
        return await RulePolicy().decide(observation)


def _trace(fail: bool = False):
    scenario = load_contract(FIXTURE / "scenario.json", Scenario)
    plan = load_contract(FIXTURE / "plan.json", Plan)
    return asyncio.run(run_agent(scenario, plan, _ScriptedGemini(fail), run_id="c" * 32))


def _status(enabled=True, left=5) -> bytes:
    return (
        LiveStatus(enabled=enabled, busy=False, daily_limit=30, runs_left_today=left)
        .model_dump_json()
        .encode()
    )


def _live(trace) -> bytes:
    return (
        LiveRun(scenario="base", trace=trace, duration_ms=1234, runs_left_today=4).model_dump_json().encode()
    )


def _reading(*roads) -> bytes:
    return json.dumps(
        {
            "scenario": "river_district",
            "reading": {"run_id": "d" * 32, "model": "gemini-3.5-flash", "closed_roads": list(roads)},
            "duration_ms": 800,
            "runs_left_today": 3,
        }
    ).encode()


# --- check_live_ai.py -------------------------------------------------------------------


@pytest.fixture
def check(monkeypatch, tmp_path):
    module = _load("check_live_ai")
    monkeypatch.setattr(module, "OUT_DIR", tmp_path)
    return module


def _argv(monkeypatch, *extra):
    url = "https://disaster-agentic-mvp-571915385983.asia-northeast1.run.app"
    monkeypatch.setattr(sys, "argv", ["check_live_ai", "--public-url", url, *extra])


def test_check_refuses_without_paid_call_confirmation():
    result = subprocess.run(
        [sys.executable, "scripts/check_live_ai.py", "--public-url", "http://127.0.0.1:8000"],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2 and "--confirm-paid-call" in result.stderr


@pytest.mark.parametrize(
    "url",
    ["https://example.com", "https://disaster-agentic-orchestrator-x.a.run.app", "http://evil.example:80"],
)
def test_check_only_targets_the_public_service(check, url):
    with pytest.raises(argparse.ArgumentTypeError):
        check.public_origin(url)


def test_check_accepts_the_public_and_local_origins(check):
    assert (
        check.public_origin("https://disaster-agentic-mvp-x.a.run.app/")
        == "https://disaster-agentic-mvp-x.a.run.app"
    )
    assert check.public_origin("http://127.0.0.1:8000") == "http://127.0.0.1:8000"


@pytest.mark.parametrize("status", [_status(enabled=False), _status(left=0), _status(left=1)])
def test_check_calls_nothing_when_live_ai_is_unavailable(check, monkeypatch, status):
    calls = []

    def fake_http(url, body=None):
        calls.append(body)
        return status

    monkeypatch.setattr(check, "_http", fake_http)
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 2
    assert calls == [None]  # the status read only, no POST


def test_check_saves_a_replayed_run_and_prints_its_logs(check, monkeypatch, tmp_path, capsys):
    answers = iter([_status(), _live(_trace()), _reading("river_bridge")])
    sent = []

    def fake_http(url, body=None):
        sent.append((url, body))
        return next(answers)

    monkeypatch.setattr(check, "_http", fake_http)
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 0
    out, err = capsys.readouterr()
    assert "outcome=verified_with_contingencies" in out
    assert 'jsonPayload.run_id="' + "c" * 32 in out
    assert "-> river_bridge" in out and 'jsonPayload.run_id="' + "d" * 32 in out
    assert err == ""
    assert sent[-1][0].endswith("/v1/agent/read")
    assert sent[-1][1] == {"scenario": "river_district", "report": check.SAMPLE_REPORT}
    saved = LiveRun.model_validate_json((tmp_path / f"live-run-{'c' * 32}.json").read_text())
    assert saved.trace.run_id == "c" * 32


def test_check_rejects_an_answer_the_core_does_not_reproduce(check, monkeypatch, tmp_path):
    data = json.loads(_live(_trace()))
    data["trace"]["steps"][0]["effect"]["input_digest"] = "f" * 64
    answers = iter([_status(), json.dumps(data).encode()])
    monkeypatch.setattr(check, "_http", lambda url, body=None: next(answers))
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 2
    assert list(tmp_path.iterdir()) == []


def test_check_reports_a_failed_gemini_call(check, monkeypatch):
    answers = iter([_status(), _live(_trace(fail=True))])
    monkeypatch.setattr(check, "_http", lambda url, body=None: next(answers))
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 3


def test_check_notes_an_unexpected_reading_without_failing(check, monkeypatch, capsys):
    answers = iter([_status(), _live(_trace()), _reading()])
    monkeypatch.setattr(check, "_http", lambda url, body=None: next(answers))
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 0
    assert "differently than expected" in capsys.readouterr().err


def test_check_reports_a_refused_reading(check, monkeypatch, capsys):
    answers = iter([_status(), _live(_trace())])

    def fake_http(url, body=None):
        if url.endswith("/v1/agent/read"):
            raise HTTPError(url, 502, "bad", None, io.BytesIO(b'{"detail":"x"}'))
        return next(answers)

    monkeypatch.setattr(check, "_http", fake_http)
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 4
    assert "report reading refused: HTTP 502" in capsys.readouterr().err


def test_check_reports_a_refused_run(check, monkeypatch, capsys):
    def fake_http(url, body=None):
        if body is None:
            return _status()
        raise HTTPError(url, 429, "busy", None, None)

    monkeypatch.setattr(check, "_http", fake_http)
    _argv(monkeypatch, "--confirm-paid-call")
    assert check.main() == 1
    assert "HTTP 429" in capsys.readouterr().err


# --- record_gemini_run.py ---------------------------------------------------------------


PRIVATE_HOST = "disaster-agentic-orchestrator-x.a.run.app"


def _attempt(kind="diagnostic", ok=True, purpose="decision", revision="abc1234", **extra):
    return gemini_attempts.Attempt(
        kind=kind,
        host=PRIVATE_HOST,
        revision=revision,
        model="gemini-3.5-flash@global",
        purpose=purpose,
        ok=ok,
        at=extra.pop("at", "2026-10-13T00:00:00+00:00"),
        **extra,
    )


@pytest.fixture
def record(monkeypatch, tmp_path):
    module = _load("record_gemini_run")
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="t.o.k\n")
    )
    out = tmp_path / "recorded.json"
    log = tmp_path / "attempts.jsonl"
    gemini_attempts.append(_attempt(), log)  # a successful one-call diagnostic on this revision
    url = "https://" + PRIVATE_HOST
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "record",
            "--service-url",
            url,
            "--out",
            str(out),
            "--confirm-paid-call",
            "--attempts-log",
            str(log),
        ],
    )
    return module, out, log


def _service(trace, posts=None):
    health = json.dumps(
        {"config": "ready", "revision": "abc1234", "model": "gemini-3.5-flash", "vertex_location": "global"}
    ).encode()

    def call(url, token, body=None):
        assert token == "t.o.k"
        if body is not None and posts is not None:
            posts.append(url)
        return health if body is None else trace.model_dump_json().encode()

    return call


def test_record_saves_a_replay_verified_gemini_run(record, monkeypatch):
    module, out, log = record
    monkeypatch.setattr(module, "_call", _service(_trace()))
    assert module.main() == 0
    saved = RecordedAgentRun.model_validate_json(out.read_text())
    assert saved.trace.policy == "vertex-gemini" and saved.service_revision == "abc1234"
    assert gemini_attempts.load(log)[-1].kind == "record" and gemini_attempts.load(log)[-1].ok


def test_record_refuses_a_run_whose_gemini_call_failed(record, monkeypatch, capsys):
    module, out, _log = record
    monkeypatch.setattr(module, "_call", _service(_trace(fail=True)))
    assert module.main() == 2
    assert not out.exists()
    err = capsys.readouterr().err
    assert "not recorded" in err and 'jsonPayload.run_id="' + "c" * 32 in err
    # The same revision and model are not paid for again until something changes.
    posts = []
    monkeypatch.setattr(module, "_call", _service(_trace(), posts))
    assert module.main() == 3
    assert posts == []


def test_a_failed_run_request_counts_as_a_failed_recording(record, monkeypatch):
    module, out, log = record

    def call(url, token, body=None):
        if body is None:
            return json.dumps(
                {
                    "config": "ready",
                    "revision": "abc1234",
                    "model": "gemini-3.5-flash",
                    "vertex_location": "global",
                }
            ).encode()
        raise HTTPError(url, 504, "timeout", None, None)

    monkeypatch.setattr(module, "_call", call)
    assert module.main() == 2
    assert not out.exists()
    assert (gemini_attempts.load(log)[-1].kind, gemini_attempts.load(log)[-1].ok) == ("record", False)
    assert module.main() == 3  # not again on this revision and model


def test_record_needs_a_successful_diagnostic_on_this_revision(record, monkeypatch, tmp_path, capsys):
    module, out, log = record
    log.write_text("")
    gemini_attempts.append(_attempt(revision="older"), log)
    posts = []
    monkeypatch.setattr(module, "_call", _service(_trace(), posts))
    assert module.main() == 3
    assert posts == [] and not out.exists()
    assert "diagnose this revision" in capsys.readouterr().err


# --- diagnose_gemini.py -----------------------------------------------------------------


def _diagnostic(ok=True, **fields) -> bytes:
    body = {
        "run_id": "e" * 32,
        "revision": "abc1234",
        "model": "gemini-3.5-flash",
        "location": "global",
        "profile": "default",
        "purpose": "decision",
        "ok": ok,
        "failed_stage": None if ok else "call",
        "error_type": None if ok else "TimeoutError",
        "status_code": None,
        "prepare_ms": 2100,
        "call_ms": 3400 if ok else 45000,
        "model_calls": 1,
        "production_call_timeout_ms": 25000,
        "diagnostics_left_today": 2,
    }
    body.update(fields)
    return json.dumps(body).encode()


@pytest.fixture
def diagnose(monkeypatch, tmp_path):
    module = _load("diagnose_gemini")
    monkeypatch.setattr(module, "_token", lambda: "t.o.k")
    log = tmp_path / "attempts.jsonl"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "diagnose",
            "--service-url",
            "https://" + PRIVATE_HOST,
            "--confirm-paid-call",
            "--attempts-log",
            str(log),
        ],
    )
    posts = []

    def serve(answer, health=None):
        def call(url, token, body=None):
            assert token == "t.o.k"
            if body is None:
                return json.dumps(
                    health
                    or {
                        "config": "ready",
                        "revision": "abc1234",
                        "model": "gemini-3.5-flash",
                        "vertex_location": "global",
                    }
                ).encode()
            posts.append(body)
            if isinstance(answer, Exception):
                raise answer
            return answer

        monkeypatch.setattr(module, "_call", call)

    return module, log, posts, serve


def test_diagnose_refuses_without_paid_call_confirmation():
    result = subprocess.run(
        [sys.executable, "scripts/diagnose_gemini.py", "--service-url", "https://" + PRIVATE_HOST],
        cwd=ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 2 and "--confirm-paid-call" in result.stderr


@pytest.mark.parametrize(
    "url",
    [
        "https://disaster-agentic-mvp-x.a.run.app",
        "http://disaster-agentic-orchestrator-x.a.run.app",
        "https://disaster-agentic-orchestrator-x.a.run.app.evil.example",
        "https://disaster-agentic-orchestrator-x.a.run.app/v1",
    ],
)
def test_diagnose_sends_the_token_only_to_the_private_service(diagnose, url):
    module, *_ = diagnose
    with pytest.raises(argparse.ArgumentTypeError):
        module.service_origin(url)
    assert module.service_origin("https://disaster-agentic-orchestrator-verify-x-an.a.run.app/") == (
        "https://disaster-agentic-orchestrator-verify-x-an.a.run.app"
    )


def test_diagnose_makes_one_request_and_reports_the_stages(diagnose, capsys):
    module, log, posts, serve = diagnose
    serve(_diagnostic())
    assert module.main() == 0
    assert posts == [
        {
            "schema_version": "gemini-diagnostic-request/v1",
            "confirm_paid_call": True,
            "purpose": "decision",
            "profile": "default",
        }
    ]
    out = capsys.readouterr().out
    assert "result: OK" in out and "2100 ms" in out and "3400 ms" in out
    assert 'jsonPayload.run_id="' + "e" * 32 in out
    [attempt] = gemini_attempts.load(log)
    assert (attempt.kind, attempt.ok, attempt.revision, attempt.run_id) == (
        "diagnostic",
        True,
        "abc1234",
        "e" * 32,
    )


def test_diagnose_calls_nothing_when_the_service_is_not_ready(diagnose):
    module, log, posts, serve = diagnose
    serve(_diagnostic(), health={"config": "misconfigured", "revision": "abc1234", "model": "invalid"})
    assert module.main() == 2
    assert posts == [] and gemini_attempts.load(log) == []


def test_a_failure_is_not_diagnosed_again_on_the_same_revision_and_model(diagnose, capsys):
    module, _log, posts, serve = diagnose
    serve(_diagnostic(ok=False))
    assert module.main() == 1
    assert "時間切れ" in capsys.readouterr().out
    assert module.main() == 3
    assert len(posts) == 1  # the second run sent nothing
    assert "already failed" in capsys.readouterr().err


def test_a_new_revision_may_be_diagnosed_after_a_failure(diagnose):
    module, log, posts, serve = diagnose
    gemini_attempts.append(_attempt(ok=False, revision="older", error_type="TimeoutError"), log)
    serve(_diagnostic())
    assert module.main() == 0
    assert len(posts) == 1


def test_a_transient_failure_gets_one_retry_after_ten_minutes(diagnose, monkeypatch):
    from datetime import UTC, datetime

    module, log, posts, serve = diagnose
    first = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    gemini_attempts.append(
        _attempt(ok=False, status_code=429, error_type="ClientError", at=first.isoformat()), log
    )
    serve(_diagnostic())
    monkeypatch.setattr(gemini_attempts, "now_utc", lambda: first.replace(minute=5))
    assert module.main() == 3 and posts == []
    monkeypatch.setattr(gemini_attempts, "now_utc", lambda: first.replace(minute=11))
    serve(_diagnostic(ok=False, status_code=429, error_type="ClientError"))
    assert module.main() == 1 and len(posts) == 1
    monkeypatch.setattr(gemini_attempts, "now_utc", lambda: first.replace(hour=12))
    assert module.main() == 3 and len(posts) == 1  # two attempts on this revision and model: stop


def test_a_refusal_before_any_call_is_not_counted_as_a_failure(diagnose):
    module, log, _posts, serve = diagnose
    serve(HTTPError("https://" + PRIVATE_HOST, 429, "limit", None, None))
    assert module.main() == 1
    assert gemini_attempts.load(log) == []


def test_a_gateway_failure_is_counted(diagnose):
    module, log, _posts, serve = diagnose
    serve(HTTPError("https://" + PRIVATE_HOST, 504, "timeout", None, None))
    assert module.main() == 1
    [attempt] = gemini_attempts.load(log)
    assert (attempt.ok, attempt.status_code) == (False, 504)


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        (
            {"ok": False, "failed_stage": "call", "error_type": "ClientError", "status_code": 403},
            "権限エラー",
        ),
        (
            {"ok": False, "failed_stage": "call", "error_type": "ClientError", "status_code": 404},
            "モデルが見つかりません",
        ),
        (
            {"ok": False, "failed_stage": "call", "error_type": "ClientError", "status_code": 400},
            "形式エラー",
        ),
        (
            {"ok": False, "failed_stage": "call", "error_type": "ServerError", "status_code": 503},
            "一時的なエラー",
        ),
        ({"ok": False, "failed_stage": "answer", "error_type": "PolicyOutputError"}, "JSON"),
        (
            {
                "ok": False,
                "failed_stage": "prepare",
                "error_type": "RefreshError",
                "call_ms": None,
                "model_calls": 0,
            },
            "要求を送る前",
        ),
        ({"call_ms": 31000}, "超えています"),
    ],
)
def test_diagnose_explains_the_failure(diagnose, fields, expected):
    from packages.domain.diagnostics import GeminiDiagnostic

    module, *_ = diagnose
    result = GeminiDiagnostic.model_validate_json(_diagnostic(**fields))
    assert expected in module.hint(result)


def test_another_profile_may_be_diagnosed_after_a_failure(diagnose, monkeypatch, capsys):
    module, log, posts, serve = diagnose
    serve(_diagnostic(ok=False, status_code=504, error_type="ServerError"))
    assert module.main() == 1
    assert "別の設定" in capsys.readouterr().out
    assert module.main() == 3  # a 504 hang is not retried with the same profile
    monkeypatch.setattr(sys, "argv", [*sys.argv, "--profile", "minimal"])
    serve(_diagnostic(profile="minimal"))
    assert module.main() == 0
    assert [body["profile"] for body in posts] == ["default", "minimal"]
    assert [a.profile for a in gemini_attempts.load(log)] == ["default", "minimal"]


def test_record_follows_the_profile_the_service_runs(record, monkeypatch, capsys):
    module, _out, log = record
    log.write_text("")
    gemini_attempts.append(_attempt(), log)  # diagnosed with "default"
    posts = []
    health = json.dumps(
        {
            "config": "ready",
            "revision": "abc1234",
            "model": "gemini-3.5-flash",
            "vertex_location": "global",
            "profile": "minimal",
        }
    ).encode()

    def call(url, token, body=None):
        if body is not None:
            posts.append(url)
        return health

    monkeypatch.setattr(module, "_call", call)
    assert module.main() == 3 and posts == []
    assert "profile" in capsys.readouterr().err


def test_another_region_may_be_diagnosed_after_shared_capacity_runs_out(diagnose, capsys):
    module, log, posts, serve = diagnose
    assert posts == []
    serve(_diagnostic(ok=False, status_code=429, error_type="ClientError"))
    assert module.main() == 1
    assert "us-central1" in capsys.readouterr().out
    assert module.main() == 3  # same endpoint within ten minutes: not yet
    serve(
        _diagnostic(location="us-central1"),
        health={
            "config": "ready",
            "revision": "abc1234",
            "model": "gemini-3.5-flash",
            "vertex_location": "us-central1",
        },
    )
    assert module.main() == 0
    assert [a.model for a in gemini_attempts.load(log)] == [
        "gemini-3.5-flash@global",
        "gemini-3.5-flash@us-central1",
    ]


# --- scripts/verify_gemini.sh: gcloud, curl and the two Python scripts are faked ----------

_FAKE_GCLOUD = """#!/usr/bin/env bash
case "$1 $2 $3" in
  "auth print-identity-token "*) echo token ;;
  "run services describe") echo https://disaster-agentic-orchestrator-verify-abc.a.run.app ;;
  "run services update")
    for arg in "$@"; do
      case "$arg" in GOOGLE_CLOUD_LOCATION=*)
        echo "${arg#*=}" > "$FAKE_STATE/location"; echo "update ${arg#*=}" >> "$FAKE_STATE/calls" ;;
      esac
    done ;;
  *) echo "unexpected gcloud $*" >> "$FAKE_STATE/calls"; exit 9 ;;
esac
"""
_FAKE_CURL = """#!/usr/bin/env bash
printf '{"config":"ready","revision":"%s","vertex_location":"%s"}' \\
  "$FAKE_REVISION" "$(cat "$FAKE_STATE/location")"
"""
_FAKE_PYTHON = """#!/usr/bin/env bash
location="$(cat "$FAKE_STATE/location")"
case "$1" in
  scripts/diagnose_gemini.py)
    echo "diagnose $location" >> "$FAKE_STATE/calls"
    case "$(sed -n "s/^$location=//p" "$FAKE_STATE/outcomes")" in
      ok) echo "result: OK"; exit 0 ;;
      refused) echo "diagnostic refused: HTTP 429" >&2; exit 1 ;;
      *) echo "error: ClientError (HTTP $(sed -n "s/^$location=//p" "$FAKE_STATE/outcomes"))"; exit 1 ;;
    esac ;;
  scripts/record_gemini_run.py) echo "record $location" >> "$FAKE_STATE/calls" ;;
  *) exec "$REAL_PYTHON" "$@" ;;
esac
"""


def _verify(tmp_path, outcomes: dict[str, str], *, confirmed: bool = True, answer: str = ""):
    import os

    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    for name, body in (("gcloud", _FAKE_GCLOUD), ("curl", _FAKE_CURL), ("python", _FAKE_PYTHON)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    (state / "location").write_text("global\n")
    (state / "outcomes").write_text("".join(f"{k}={v}\n" for k, v in outcomes.items()))
    revision = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, check=True, capture_output=True, text=True
    ).stdout.strip()
    env = {k: v for k, v in os.environ.items() if k not in {"CONFIRM_PAID", "GCP_PROJECT_ID"}}
    env |= {
        "PATH": f"{bin_dir}:{env['PATH']}",
        "PYTHON": str(bin_dir / "python"),
        "REAL_PYTHON": sys.executable,
        "FAKE_STATE": str(state),
        "FAKE_REVISION": revision,  # already deployed: the deploy step is skipped
        "VERIFY_LOG_DIR": str(tmp_path / "logs"),
    }
    if confirmed:
        env["CONFIRM_PAID"] = "yes"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "verify_gemini.sh")],
        input=answer,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    calls_file = state / "calls"
    calls = calls_file.read_text().split("\n")[:-1] if calls_file.exists() else []
    return result, calls


@pytest.mark.parametrize(
    ("outcomes", "expected"),
    [
        ({"global": "ok"}, ["diagnose global", "record global"]),
        # Shared capacity (429) or no model (404): the next endpoint, at most three in all.
        (
            {"global": "429", "us-central1": "ok"},
            ["diagnose global", "update us-central1", "diagnose us-central1", "record us-central1"],
        ),
        (
            {"global": "429", "us-central1": "404", "asia-northeast1": "429"},
            [
                "diagnose global",
                "update us-central1",
                "diagnose us-central1",
                "update asia-northeast1",
                "diagnose asia-northeast1",
            ],
        ),
        # Anything else is not repeated elsewhere, nor is the service's own refusal.
        ({"global": "504"}, ["diagnose global"]),
        ({"global": "403"}, ["diagnose global"]),
        ({"global": "refused"}, ["diagnose global"]),
    ],
)
def test_verify_switches_endpoints_only_for_capacity_and_records_after_a_good_diagnostic(
    tmp_path, outcomes, expected
):
    result, calls = _verify(tmp_path, outcomes)
    assert calls == expected, result.stdout + result.stderr
    recorded = any(call.startswith("record") for call in calls)
    assert (result.returncode == 0) == recorded
    assert "記録: " + ("yes" if recorded else "no") in result.stdout
    assert list((tmp_path / "logs").glob("verify-*.log"))


def test_verify_calls_nothing_paid_without_a_yes(tmp_path):
    result, calls = _verify(tmp_path, {"global": "ok"}, confirmed=False, answer="no\n")
    assert calls == []
    assert "Geminiは呼んでいません" in result.stdout


def test_verify_stays_on_the_verify_service_and_the_allowed_endpoints():
    from apps.orchestrator_service import main as orchestrator

    script = (ROOT / "scripts" / "verify_gemini.sh").read_text(encoding="utf-8")
    code = "\n".join(line for line in script.splitlines() if not line.lstrip().startswith("#"))
    for untouched in ("LIVE_AI_ENABLED", "deploy_cloud_run_mvp", "disaster-agentic-mvp"):
        assert untouched not in code
    assert 'SERVICE="disaster-agentic-orchestrator-verify"' in code
    locations = code.split("LOCATIONS=(", 1)[1].split(")", 1)[0].split()
    assert locations[0] == orchestrator.ALLOWED_LOCATION
    assert set(locations) == orchestrator.ALLOWED_LOCATIONS


def _python_check(script: str, marker: str) -> str:
    """The inline Python of the shell step that starts with marker."""
    return script.split(marker, 1)[1].split("python3 -c '", 1)[1].split("\n'", 1)[0]


@pytest.mark.parametrize(("served", "ok"), [("35dc0c3", True), ("57ca00d", False)])
def test_public_deploy_fails_when_the_url_serves_another_revision(served, ok):
    script = (ROOT / "scripts" / "deploy_cloud_run_mvp.sh").read_text(encoding="utf-8")
    deploy_at = script.index("gcloud run deploy")
    # Traffic follows the new revision even if an older one was pinned (a tagged preview).
    assert script.index("--to-latest") > deploy_at
    check = _python_check(script, '"${URL}/health"')
    result = subprocess.run(
        [sys.executable, "-c", check],
        input=json.dumps({"status": "ok", "revision": served}),
        env={"EXPECTED": "35dc0c3"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == ok, result.stderr


@pytest.mark.parametrize(("status", "ok"), [("replay_verified", True), ("absent", False)])
def test_public_deploy_requires_the_committed_recording_to_replay(status, ok):
    script = (ROOT / "scripts" / "deploy_cloud_run_mvp.sh").read_text(encoding="utf-8")
    assert 'if [[ -f data/evidence/recorded_gemini_run.json ]]; then RECORDED="replay_verified"; fi' in script
    check = _python_check(script, '"${URL}/v1/showcase"')
    showcase = {
        "schema_version": "evacweave-showcase/v2",
        "agent_trace": {"outcome": "verified_with_contingencies"},
        "more_runs": [{"key": "river_district"}],
        "recorded_ai_run_status": status,
    }
    result = subprocess.run(
        [sys.executable, "-c", check],
        input=json.dumps(showcase),
        env={"RECORDED": "replay_verified"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) == ok, result.stderr
