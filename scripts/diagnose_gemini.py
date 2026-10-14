"""Find out why live Gemini runs fail: one diagnostic request to the private service.

PAID: the private service sends at most ONE billable Gemini request on Vertex AI, the
same request as the agent loop's first decision (or, with --purpose report_read, as a
report reading). The script refuses to start without --confirm-paid-call and, at a
terminal, asks once more (CONFIRM_PAID=yes skips the question).

It reports which stage failed (preparing the client, the request, or the answer), the
exception class, the HTTP status when there is one, and how long each stage took
against the production limit. Nothing else: no prompt, no answer, no token.

--profile picks how the request asks for the answer (packages/agents/orchestrator.py,
REQUEST_PROFILES): default (schema, thinking LOW), minimal (schema, thinking MINIMAL)
or json (no schema, thinking LOW). Without it, the service's own profile is used.

It does not repeat a failure: the same revision, model and profile are not diagnosed
again after a failed attempt (one retry after ten minutes for a transient 429/500/503).
Try another profile, deploy a fix or switch the model first. Attempts are kept in
artifacts/diagnostics/gemini-attempts.jsonl (packages/services/gemini_attempts.py).

  python3 scripts/diagnose_gemini.py \\
    --service-url https://disaster-agentic-orchestrator-verify-XXXX.asia-northeast1.run.app \\
    --confirm-paid-call
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from packages.domain.diagnostics import GeminiDiagnostic  # noqa: E402
from packages.domain.io import parse_contract  # noqa: E402
from packages.services import gemini_attempts  # noqa: E402

SERVICE_PREFIX = "disaster-agentic-orchestrator"
PROJECT = "physical-ai-guardian"
PROFILES = ("default", "minimal", "json")
MAX_RESPONSE_BYTES = 65_536
# The private service answers within its Cloud Run timeout (60 s).
HTTP_TIMEOUT_S = 75


class _NoRedirect(HTTPRedirectHandler):
    """urllib would forward the Authorization header to a redirect target; refuse instead."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


_OPENER = build_opener(_NoRedirect)


def service_origin(url: str) -> str:
    # The identity token is general purpose: send it only to the private agent service.
    parts = urlsplit(url.strip().rstrip("/"))
    host = parts.hostname or ""
    if (
        parts.scheme != "https"
        or not host.startswith(SERVICE_PREFIX + "-")
        or not host.endswith(".run.app")
        or parts.port is not None
        or parts.username
        or parts.password
        or parts.path
        or parts.query
        or parts.fragment
    ):
        raise argparse.ArgumentTypeError(
            f"use the private Cloud Run service origin (https://{SERVICE_PREFIX}-….run.app)"
        )
    return f"https://{host}"


def _token() -> str:
    return subprocess.run(
        ["gcloud", "auth", "print-identity-token"], check=True, capture_output=True, text=True
    ).stdout.strip()


def _call(url: str, token: str, body: dict[str, Any] | None = None) -> bytes:
    request = Request(  # noqa: S310 - origin validated by service_origin()
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method="GET" if body is None else "POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with _OPENER.open(request, timeout=HTTP_TIMEOUT_S) as response:  # 3xx raises, never followed
        raw: bytes = response.read(MAX_RESPONSE_BYTES + 1)
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ValueError("response too large")
    return raw


def logging_query(run_id: str) -> str:
    return (
        "gcloud logging read "
        f'\'resource.type="cloud_run_revision" AND jsonPayload.run_id="{run_id}"\' '
        f"--project {PROJECT} --limit 20 --format json"
    )


def hint(result: GeminiDiagnostic) -> str:
    """What the result most likely means, and what to change before trying again."""
    limit_s = result.production_call_timeout_ms // 1000
    if result.ok:
        if result.call_ms is not None and result.call_ms > result.production_call_timeout_ms:
            return (
                f"応答は来ましたが、本番の1回あたりの制限（{limit_s}秒）を超えています。"
                "モデルの応答の遅さが原因です。"
            )
        return f"本番の制限（{limit_s}秒）内に応答しました。record_gemini_run.py で記録できます。"
    if result.failed_stage == "prepare":
        return (
            "要求を送る前（SDKの読み込み・認証情報の取得）で失敗しました。"
            "実行サービスアカウントと、google-auth・google-genai の導入を確認してください。"
        )
    if result.failed_stage == "answer":
        return (
            "応答は来ましたが、決めた形式（JSON）ではありません。"
            "出力の上限（思考を含む）に達した可能性があります。"
        )
    code = result.status_code
    if code in (401, 403):
        return (
            "権限エラーです。実行サービスアカウントに guardianVertexGenerate"
            "（aiplatform.endpoints.predict）が付いているか、Vertex AI API が有効かを"
            "確認してください（./scripts/setup_gemini_iam.sh）。"
        )
    if code == 404:
        return (
            "このエンドポイントにモデルが見つかりません。VERTEX_GEMINI_MODEL と"
            " GOOGLE_CLOUD_LOCATION（global・us-central1・asia-northeast1）を確認してください。"
        )
    if code == 400:
        return "要求の形式エラーです。思考レベルや応答スキーマをモデルが受け付けていない可能性があります。"
    if code == 504:
        return (
            f"Vertex AIが期限内に応答を返せませんでした（{result.call_ms} ms で打ち切り）。"
            "一時的な混雑ではなく、この要求の設定で応答が止まっている可能性が高いので、"
            "同じ設定では繰り返しません。--profile で別の設定を診断してください"
            "（default → minimal → json の順）。"
        )
    if code == 429:
        return (
            "Vertex AIの共有の処理枠が混んでいます（このプロジェクトの上限ではありません）。"
            "10分後に1回だけ再試行できます。続く場合は GOOGLE_CLOUD_LOCATION を"
            " us-central1 や asia-northeast1 に変えて診断してください。"
        )
    if code in gemini_attempts.TRANSIENT_STATUS:
        return "一時的なエラーです。10分後に1回だけ再試行できます。"
    if result.error_type and "Timeout" in result.error_type:
        return (
            f"時間切れです（{result.call_ms} ms）。診断用の長い制限でも応答がありません。"
            "下りの通信か、モデル側の停滞を疑ってください。"
        )
    return "上の例外クラスとHTTPステータスで原因を確認してください。"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--service-url", required=True, type=service_origin)
    parser.add_argument("--purpose", choices=("decision", "report_read"), default="decision")
    parser.add_argument("--profile", choices=PROFILES, help="request profile (default: the service's own)")
    parser.add_argument(
        "--confirm-paid-call", action="store_true", help="acknowledge one billable Gemini call"
    )
    parser.add_argument(
        "--attempts-log", type=Path, default=gemini_attempts.DEFAULT_LOG, help=argparse.SUPPRESS
    )
    args = parser.parse_args()
    if not args.confirm_paid_call:
        parser.error("a diagnostic makes one billable Gemini call; pass --confirm-paid-call to proceed")

    token = _token()
    health = json.loads(_call(args.service_url + "/health", token))
    if health.get("config") != "ready":
        print("private service is not configured; nothing was called", file=sys.stderr)
        return 2
    host = urlsplit(args.service_url).hostname or ""
    revision = str(health.get("revision", "unknown"))
    # The endpoint is part of what is attempted: another region is another request.
    model = f"{health.get('model', 'unknown')}@{health.get('vertex_location', 'unknown')}"
    profile = args.profile or str(health.get("profile", "default"))
    refusal = gemini_attempts.diagnostic_refusal(
        gemini_attempts.load(args.attempts_log),
        host,
        revision,
        model,
        args.purpose,
        profile,
        gemini_attempts.now_utc(),
    )
    if refusal:
        print(f"not run: {refusal}.", file=sys.stderr)
        print(
            "Try another --profile, deploy a fix (a new revision) or switch VERTEX_GEMINI_MODEL.",
            file=sys.stderr,
        )
        return 3

    print(f"service={host} revision={revision} model={model} profile={profile} purpose={args.purpose}")
    print("This sends ONE billable Gemini request.")
    asked = sys.stdin.isatty() and os.environ.get("CONFIRM_PAID") != "yes"
    if asked and input("Type yes to continue: ").strip() != "yes":
        print("cancelled; nothing was called")
        return 2

    try:
        raw = _call(
            args.service_url + "/v1/diagnostics/gemini",
            token,
            {
                "schema_version": "gemini-diagnostic-request/v1",
                "confirm_paid_call": True,
                "purpose": args.purpose,
                "profile": profile,
            },
        )
    except HTTPError as exc:
        print(f"diagnostic refused: HTTP {exc.code}", file=sys.stderr)
        if exc.code != 429:  # 429: refused before any call (limit, or one already running)
            gemini_attempts.append(
                gemini_attempts.Attempt(
                    kind="diagnostic",
                    host=host,
                    revision=revision,
                    model=model,
                    purpose=args.purpose,
                    ok=False,
                    at=gemini_attempts.now_utc().isoformat(),
                    error_type="HTTPError",
                    status_code=exc.code,
                    profile=profile,
                ),
                args.attempts_log,
            )
        return 1
    result = parse_contract(raw, GeminiDiagnostic)
    gemini_attempts.append(
        gemini_attempts.Attempt(
            kind="diagnostic",
            host=host,
            revision=result.revision,
            model=f"{result.model}@{result.location}",
            purpose=result.purpose,
            ok=result.ok,
            at=gemini_attempts.now_utc().isoformat(),
            run_id=result.run_id,
            error_type=result.error_type,
            status_code=result.status_code,
            profile=result.profile,
        ),
        args.attempts_log,
    )
    call = "not sent" if result.call_ms is None else f"{result.call_ms} ms"
    print(f"result: {'OK' if result.ok else 'FAILED at ' + str(result.failed_stage)}")
    print(f"prepare (SDK, credentials): {result.prepare_ms} ms; request: {call}")
    limit_ms = result.production_call_timeout_ms
    print(f"production limit per request: {limit_ms} ms; calls made: {result.model_calls}")
    if not result.ok:
        print(f"error: {result.error_type} (HTTP {result.status_code or '-'})")
    print(hint(result))
    print(f"diagnostics left today on this service: {result.diagnostics_left_today}")
    print("Cloud Logging entries of this diagnostic:")
    print("  " + logging_query(result.run_id))
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
