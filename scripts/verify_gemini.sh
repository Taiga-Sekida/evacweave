#!/usr/bin/env bash
# 検証用の非公開サービスで、Geminiが実際に動くかを一括で確かめる（Cloud Shell用）。
#
#   1. 検証用サービス disaster-agentic-orchestrator-verify をこの版でデプロイ（課金なし。
#      すでにこの版なら省略）
#   2. Geminiの診断（1回ずつ）。共有枠の混雑（HTTP 429）かモデル未提供（HTTP 404）のときだけ、
#      Vertex AIのエンドポイントを global → us-central1 → asia-northeast1 の順に切り替えて
#      診断し直す                                                    ※課金あり：最大3回
#   3. 診断が通ったエンドポイントで1回実行して記録
#      → data/evidence/recorded_gemini_run.json                       ※課金あり：最大6回
#
# 課金の前に1回だけ確認します（yes と入力。CONFIRM_PAID=yes で入力を省略）。
# 混雑以外の失敗（504・権限・形式など）は別のエンドポイントで繰り返さず、そこで止めます。
# 同じ版・エンドポイント・設定で失敗した診断は、diagnose_gemini.py が繰り返しません。
# 公開サービス、本番の非公開サービス、LIVE_AI_ENABLED には触れません。
# 出力は artifacts/diagnostics/verify-<時刻>.log にも残ります。
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"
PROJECT="physical-ai-guardian"
REGION="${GCP_REGION:-asia-northeast1}"
SERVICE="disaster-agentic-orchestrator-verify"
# apps/orchestrator_service/main.py ALLOWED_LOCATIONS; the first is the default.
LOCATIONS=(global us-central1 asia-northeast1)
PYTHON="${PYTHON:-python3}"
if [[ "${GCP_PROJECT_ID:-${PROJECT}}" != "${PROJECT}" ]]; then
  echo "別プロジェクトには実行しません: ${GCP_PROJECT_ID}" >&2
  exit 2
fi

LOG_DIR="${VERIFY_LOG_DIR:-${ROOT_DIR}/artifacts/diagnostics}"
mkdir -p "${LOG_DIR}"
LOG="${LOG_DIR}/verify-$(date -u +%Y%m%dT%H%M%SZ).log"
exec > >(tee -a "${LOG}") 2>&1

step() { printf '\n==== %s ====\n' "$1"; }
service_url() {
  gcloud run services describe "${SERVICE}" --project "${PROJECT}" --region "${REGION}" \
    --format='value(status.url)' 2>/dev/null
}
# One field of the private service's /health (free: no Gemini call).
health_field() {
  curl -sS --fail --max-time 30 -H "Authorization: Bearer ${TOKEN}" "${URL}/health" \
    | "${PYTHON}" -c 'import json, sys; print(json.load(sys.stdin).get(sys.argv[1], ""))' "$1"
}

REVISION="$(git rev-parse --short HEAD)"
TOKEN="$(gcloud auth print-identity-token)"

step "1/3 検証用サービスをこの版にする（課金なし）"
echo "版: $(git log --oneline -1)"
URL="$(service_url || true)"
if [[ -n "${URL}" && "$(health_field revision || true)" == "${REVISION}" ]]; then
  echo "${SERVICE} はすでに ${REVISION} です。デプロイを省略します。"
elif ! CLOUD_RUN_ORCHESTRATOR_SERVICE="${SERVICE}" VERTEX_LOCATION="${LOCATIONS[0]}" \
  ./scripts/deploy_cloud_run_orchestrator.sh; then
  echo "デプロイに失敗しました。Geminiは呼んでいません。" >&2
  exit 1
fi
URL="$(service_url)"
echo "URL: ${URL}"

step "2/3 Geminiの診断"
echo "課金: 診断は1回ずつ最大${#LOCATIONS[@]}回（429・404のときだけ次のエンドポイントへ）、"
echo "      診断が通ったら記録で最大6回。合計で最大$((${#LOCATIONS[@]} + 6))回です。"
if [[ "${CONFIRM_PAID:-}" != "yes" ]]; then
  read -r -p "実行するには yes と入力: " answer
  if [[ "${answer}" != "yes" ]]; then
    echo "中止しました。Geminiは呼んでいません。"
    exit 0
  fi
fi

OK_LOCATION=""
RESULT="$(mktemp)"
trap 'rm -f "${RESULT}"' EXIT
for location in "${LOCATIONS[@]}"; do
  if [[ "$(health_field vertex_location)" != "${location}" ]]; then
    echo "Vertex AIのエンドポイントを ${location} に切り替えます（課金なし）"
    if ! gcloud run services update "${SERVICE}" --project "${PROJECT}" --region "${REGION}" \
      --update-env-vars "GOOGLE_CLOUD_LOCATION=${location}" --quiet; then
      echo "切り替えに失敗しました。" >&2
      break
    fi
  fi
  echo "-- 診断: ${location}"
  CONFIRM_PAID=yes "${PYTHON}" scripts/diagnose_gemini.py --service-url "${URL}" --confirm-paid-call \
    2>&1 | tee "${RESULT}"
  if [[ "${PIPESTATUS[0]}" == "0" ]]; then
    OK_LOCATION="${location}"
    break
  fi
  # Vertex AI said 429 or 404 (now, or on an earlier attempt the script refused to repeat).
  # Not "diagnostic refused: HTTP 429": that is the service's own limit, before any call.
  if grep -qE '\(HTTP (429|404)[,)]' "${RESULT}"; then
    echo "→ 混雑かモデル未提供なので、次のエンドポイントで診断します。"
    continue
  fi
  echo "→ 混雑以外の失敗なので、別のエンドポイントでは繰り返さず、ここで止めます。"
  break
done

RECORDED="no"
if [[ -n "${OK_LOCATION}" ]]; then
  step "3/3 Geminiで1回実行して記録"
  if "${PYTHON}" scripts/record_gemini_run.py --service-url "${URL}" --confirm-paid-call; then
    RECORDED="yes"
  fi
fi

step "結果"
echo "版: ${REVISION}"
echo "診断が通ったエンドポイント: ${OK_LOCATION:-なし}"
echo "記録: ${RECORDED}"
echo "ログ: ${LOG}"
if [[ "${RECORDED}" == "yes" ]]; then
  cat <<EOF

記録をPRのブランチへ:
  git add data/evidence/recorded_gemini_run.json
  git commit -m "evidence: record a Gemini run on Vertex AI"
  git push origin HEAD
本番の非公開サービスも同じエンドポイントでデプロイします: VERTEX_LOCATION=${OK_LOCATION}
EOF
fi
[[ "${RECORDED}" == "yes" ]]
