#!/usr/bin/env bash
set -euo pipefail

# Always deploy the repository root, even when this script is invoked from another directory.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-physical-ai-guardian}"
GCP_REGION="${GCP_REGION:-asia-northeast1}"
CLOUD_RUN_SERVICE="${CLOUD_RUN_SERVICE:-disaster-agentic-mvp}"
REVISION="${APP_REVISION:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"
# Runtime identity without any Vertex AI permission (created by setup_gemini_iam.sh).
PUBLIC_SERVICE_ACCOUNT="${PUBLIC_SERVICE_ACCOUNT:-evacweave-public-sa@${GCP_PROJECT_ID}.iam.gserviceaccount.com}"
ORCHESTRATOR_SERVICE="${CLOUD_RUN_ORCHESTRATOR_SERVICE:-disaster-agentic-orchestrator}"
# Live Gemini runs bill the project, so they are opt-in: LIVE_AI_ENABLED=1.
LIVE_AI_ENABLED="${LIVE_AI_ENABLED:-0}"
LIVE_AI_DAILY_RUNS="${LIVE_AI_DAILY_RUNS:-40}"
# The daily count lives here, so a new instance does not reset it (scripts/setup_live_quota.sh).
LIVE_AI_QUOTA_BUCKET="${LIVE_AI_QUOTA_BUCKET:-${GCP_PROJECT_ID}-evacweave-live-quota}"
ORCHESTRATOR_URL=""

echo "Source  : ${ROOT_DIR}"
echo "Project : ${GCP_PROJECT_ID}"
echo "Region  : ${GCP_REGION}"
echo "Service : ${CLOUD_RUN_SERVICE}"
echo "Identity: ${PUBLIC_SERVICE_ACCOUNT}"
echo "Live AI : ${LIVE_AI_ENABLED} (daily runs ${LIVE_AI_DAILY_RUNS})"
echo "Revision: ${REVISION}"

gcloud config set project "${GCP_PROJECT_ID}"

# Remember what is serving now, so a failed smoke check below can be rolled back.
PREVIOUS_REVISION="$(gcloud run services describe "${CLOUD_RUN_SERVICE}" --project "${GCP_PROJECT_ID}" \
  --region "${GCP_REGION}" --format='value(status.latestReadyRevisionName)' 2>/dev/null || true)"
rollback_hint() {
  if [[ -n "${PREVIOUS_REVISION}" ]]; then
    echo "Smoke check failed. To serve the previous revision again:" >&2
    echo "  gcloud run services update-traffic ${CLOUD_RUN_SERVICE} --project ${GCP_PROJECT_ID} --region ${GCP_REGION} --to-revisions=${PREVIOUS_REVISION}=100" >&2
  fi
}

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  storage.googleapis.com

if ! gcloud iam service-accounts describe "${PUBLIC_SERVICE_ACCOUNT}" --project "${GCP_PROJECT_ID}" >/dev/null 2>&1; then
  echo "Missing ${PUBLIC_SERVICE_ACCOUNT}. Run ./scripts/setup_gemini_iam.sh first." >&2
  exit 3
fi
if [[ "${LIVE_AI_ENABLED}" == "1" ]]; then
  ORCHESTRATOR_URL="$(gcloud run services describe "${ORCHESTRATOR_SERVICE}" --project "${GCP_PROJECT_ID}" \
    --region "${GCP_REGION}" --format='value(status.url)' 2>/dev/null || true)"
  if [[ -z "${ORCHESTRATOR_URL}" ]]; then
    echo "LIVE_AI_ENABLED=1 needs the private service. Run ./scripts/deploy_cloud_run_orchestrator.sh first." >&2
    exit 3
  fi
  # The public identity may invoke the private service, and nothing else.
  gcloud run services add-iam-policy-binding "${ORCHESTRATOR_SERVICE}" --project "${GCP_PROJECT_ID}" \
    --region "${GCP_REGION}" --member "serviceAccount:${PUBLIC_SERVICE_ACCOUNT}" --role roles/run.invoker \
    --quiet >/dev/null
  echo "Private service: ${ORCHESTRATOR_URL} (run.invoker granted to ${PUBLIC_SERVICE_ACCOUNT})"
  LIVE_AI_QUOTA_BUCKET="${LIVE_AI_QUOTA_BUCKET}" PUBLIC_SERVICE_ACCOUNT="${PUBLIC_SERVICE_ACCOUNT}" \
    ./scripts/setup_live_quota.sh
elif [[ "${LIVE_AI_ENABLED}" != "0" ]]; then
  echo "LIVE_AI_ENABLED must be 0 or 1" >&2
  exit 2
fi

ENV_VARS="APP_REVISION=${REVISION},PUBLIC_SIMULATION_ENABLED=0,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT_ID}"
ENV_VARS+=",LIVE_AI_ENABLED=${LIVE_AI_ENABLED},LIVE_AI_DAILY_RUNS=${LIVE_AI_DAILY_RUNS}"
if [[ -n "${ORCHESTRATOR_URL}" ]]; then
  ENV_VARS+=",ORCHESTRATOR_URL=${ORCHESTRATOR_URL},LIVE_AI_QUOTA_BUCKET=${LIVE_AI_QUOTA_BUCKET}"
fi

# 90 s: a live Gemini run may take the private service's whole 60 s timeout plus its cold start
# (apps/api/live_agent.py waits up to 70 s; tests keep the numbers in step).
gcloud run deploy "${CLOUD_RUN_SERVICE}" \
  --project "${GCP_PROJECT_ID}" \
  --source "${ROOT_DIR}" \
  --region "${GCP_REGION}" \
  --allow-unauthenticated \
  --service-account "${PUBLIC_SERVICE_ACCOUNT}" \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 4 \
  --timeout 90s \
  --min 0 \
  --max 1 \
  --cpu-throttling \
  --no-cpu-boost \
  --set-env-vars "${ENV_VARS}" \
  --quiet

# A split pinned to an older revision (e.g. a tagged preview) would keep serving it at the URL.
gcloud run services update-traffic "${CLOUD_RUN_SERVICE}" --project "${GCP_PROJECT_ID}" \
  --region "${GCP_REGION}" --to-latest --quiet >/dev/null

URL="$(gcloud run services describe "${CLOUD_RUN_SERVICE}" \
  --project "${GCP_PROJECT_ID}" \
  --region "${GCP_REGION}" \
  --format='value(status.url)')"

trap rollback_hint ERR
# The URL must serve what was just deployed, not an older revision.
curl --fail --silent --show-error --retry 10 --retry-delay 2 "${URL}/health" | EXPECTED="${REVISION}" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
assert doc.get("revision") == os.environ["EXPECTED"], "URL serves %s, deployed %s" % (doc.get("revision"), os.environ["EXPECTED"])
print("serving revision", doc["revision"])
'

# Smoke-check what judges will open: the agent showcase, the lab and the API guide.
# The committed Gemini recording must pass the public replay check.
RECORDED="absent"
if [[ -f data/evidence/recorded_gemini_run.json ]]; then RECORDED="replay_verified"; fi
curl --fail --silent --show-error "${URL}/v1/showcase" | RECORDED="${RECORDED}" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
assert doc["schema_version"] == "evacweave-showcase/v2", doc.get("schema_version")
assert doc["agent_trace"]["outcome"] == "verified_with_contingencies", doc["agent_trace"]["outcome"]
assert [run["key"] for run in doc["more_runs"]] == ["river_district"]
assert doc["recorded_ai_run_status"] == os.environ["RECORDED"], doc["recorded_ai_run_status"]
print("showcase ok; recorded Gemini run:", doc["recorded_ai_run_status"])
'
for page in / /slides?shot=9 /lab /api-guide; do
  curl --fail --silent --show-error --output /dev/null "${URL}${page}"
done
# Status only: this makes no Gemini call.
curl --fail --silent --show-error "${URL}/v1/agent/live" | LIVE_AI_ENABLED="${LIVE_AI_ENABLED}" python3 -c '
import json, os, sys
doc = json.load(sys.stdin)
assert doc["enabled"] == (os.environ["LIVE_AI_ENABLED"] == "1"), doc
print("live Gemini:", "on" if doc["enabled"] else "off", "| runs left today:", doc["runs_left_today"])
'
printf '\nCloud Run URL: %s\n' "${URL}"
