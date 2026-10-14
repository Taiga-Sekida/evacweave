#!/usr/bin/env bash
# The bucket that keeps the public service's daily live-Gemini count (apps/api/live_quota.py),
# so a new Cloud Run instance does not start counting again at 0. No Gemini call.
#
# Safe to run again. The bucket is private (uniform access, public access prevented); the
# public service identity may read and write objects in this bucket only, and holds no
# project role. Each day's count is one object of a few bytes, deleted after 30 days.
# scripts/deploy_cloud_run_mvp.sh runs this when LIVE_AI_ENABLED=1.
set -euo pipefail

PROJECT="physical-ai-guardian"
REGION="${GCP_REGION:-asia-northeast1}"
BUCKET="${LIVE_AI_QUOTA_BUCKET:-${PROJECT}-evacweave-live-quota}"
PUBLIC_SERVICE_ACCOUNT="${PUBLIC_SERVICE_ACCOUNT:-evacweave-public-sa@${PROJECT}.iam.gserviceaccount.com}"
if [[ "${GCP_PROJECT_ID:-${PROJECT}}" != "${PROJECT}" ]]; then
  echo "別プロジェクトには作りません: ${GCP_PROJECT_ID}" >&2
  exit 2
fi

if ! gcloud storage buckets describe "gs://${BUCKET}" --project "${PROJECT}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --project "${PROJECT}" --location "${REGION}" \
    --default-storage-class STANDARD --uniform-bucket-level-access --public-access-prevention
fi
LIFECYCLE="$(mktemp)"
trap 'rm -f "${LIFECYCLE}"' EXIT
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}' > "${LIFECYCLE}"
gcloud storage buckets update "gs://${BUCKET}" --project "${PROJECT}" --lifecycle-file "${LIFECYCLE}" >/dev/null
# On this bucket only: read the day's count, and replace it (create + delete).
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --project "${PROJECT}" \
  --member "serviceAccount:${PUBLIC_SERVICE_ACCOUNT}" --role roles/storage.objectUser >/dev/null
echo "Live quota bucket: gs://${BUCKET} (roles/storage.objectUser for ${PUBLIC_SERVICE_ACCOUNT} on this bucket only)"
