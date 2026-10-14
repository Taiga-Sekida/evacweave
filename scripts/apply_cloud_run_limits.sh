#!/usr/bin/env bash
set -euo pipefail

PROJECT="physical-ai-guardian"
REGION="${GCP_REGION:-asia-northeast1}"
PUBLIC="disaster-agentic-mvp"
PRIVATE="disaster-agentic-orchestrator"

if [[ "${GCP_PROJECT_ID:-${PROJECT}}" != "${PROJECT}" ]]; then
  echo "別プロジェクトには適用しません: ${GCP_PROJECT_ID}" >&2
  exit 2
fi
command -v gcloud >/dev/null || { echo "gcloud が必要です" >&2; exit 2; }

echo "公開サービスの負荷制限を適用: ${PROJECT}/${REGION}/${PUBLIC}"
gcloud run services update "${PUBLIC}" \
  --project "${PROJECT}" \
  --region "${REGION}" \
  --min=0 \
  --max=1 \
  --concurrency=4 \
  --timeout=90s \
  --cpu-throttling \
  --no-cpu-boost \
  --update-env-vars "PUBLIC_SIMULATION_ENABLED=0"

# Nonexistent service must not be created accidentally by this safeguard.
if gcloud run services describe "${PRIVATE}" \
  --project "${PROJECT}" --region "${REGION}" --format="value(metadata.name)" >/dev/null 2>&1; then
  echo "非公開サービスの負荷制限を適用: ${PRIVATE}"
  gcloud run services update "${PRIVATE}" \
    --project "${PROJECT}" \
    --region "${REGION}" \
    --min=0 \
    --max=1 \
    --concurrency=2 \
    --timeout=60s \
    --cpu-throttling
else
  echo "非公開サービスは未作成です。変更を省略します。"
fi

echo "設定後の公開Cloud Run:"
gcloud run services describe "${PUBLIC}" \
  --project "${PROJECT}" --region "${REGION}" \
  --format="yaml(metadata.annotations,spec.template.spec.containerConcurrency,spec.template.spec.timeoutSeconds)"

echo "注意: これらは支出の厳密な上限ではありません。Cloud Billingの支出上限予算も設定してください。"
