#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

GCP_PROJECT_ID="${GCP_PROJECT_ID:-physical-ai-guardian}"
GCP_REGION="${GCP_REGION:-asia-northeast1}"
CLOUD_RUN_SERVICE="${CLOUD_RUN_ORCHESTRATOR_SERVICE:-disaster-agentic-orchestrator}"
RUNTIME_SERVICE_ACCOUNT="${ORCHESTRATOR_SERVICE_ACCOUNT:-guardian-agent-sa@physical-ai-guardian.iam.gserviceaccount.com}"
VERTEX_GEMINI_MODEL="${VERTEX_GEMINI_MODEL:-gemini-3.5-flash}"
# Request profile (packages/agents/orchestrator.py, REQUEST_PROFILES): default | minimal | json.
GEMINI_PROFILE="${GEMINI_PROFILE:-default}"
# Agent decisions think MINIMAL (fast enum choices); readings use GEMINI_PROFILE.
DECISION_PROFILE="${DECISION_PROFILE:-minimal}"
# Vertex AI endpoint: global (default), us-central1 or asia-northeast1.
VERTEX_LOCATION="${VERTEX_LOCATION:-global}"
REVISION="${APP_REVISION:-$(git rev-parse --short HEAD 2>/dev/null || echo manual)}"

if [[ "${GCP_PROJECT_ID}" != "physical-ai-guardian" ]]; then
  echo "Refusing to deploy orchestrator to unexpected project: ${GCP_PROJECT_ID}" >&2
  exit 2
fi

echo "Source   : ${ROOT_DIR}"
echo "Project  : ${GCP_PROJECT_ID}"
echo "Region   : ${GCP_REGION}"
echo "Service  : ${CLOUD_RUN_SERVICE}"
echo "RuntimeSA: ${RUNTIME_SERVICE_ACCOUNT}"
echo "Model    : ${VERTEX_GEMINI_MODEL}"
echo "Profile  : ${GEMINI_PROFILE} (decisions: ${DECISION_PROFILE})"
echo "Vertex   : ${VERTEX_LOCATION}"
echo "Revision : ${REVISION}"

gcloud config set project "${GCP_PROJECT_ID}"

gcloud services enable \
  run.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com \
  aiplatform.googleapis.com

gcloud iam service-accounts describe "${RUNTIME_SERVICE_ACCOUNT}" \
  --project "${GCP_PROJECT_ID}" >/dev/null

ROLE_PERMISSIONS="$(gcloud iam roles describe guardianVertexGenerate \
  --project "${GCP_PROJECT_ID}" \
  --format='value(includedPermissions)' 2>/dev/null || true)"

if [[ "${ROLE_PERMISSIONS}" != *"aiplatform.endpoints.predict"* ]]; then
  echo "guardianVertexGenerate must include aiplatform.endpoints.predict" >&2
  exit 3
fi

# One image serves both services (Dockerfile); APP_MODULE selects this one.
ENV_VARS="APP_MODULE=apps.orchestrator_service.main:app,GOOGLE_CLOUD_PROJECT=${GCP_PROJECT_ID}"
ENV_VARS+=",GOOGLE_CLOUD_LOCATION=${VERTEX_LOCATION},VERTEX_GEMINI_MODEL=${VERTEX_GEMINI_MODEL}"
ENV_VARS+=",GEMINI_PROFILE=${GEMINI_PROFILE},DECISION_PROFILE=${DECISION_PROFILE},APP_REVISION=${REVISION}"

gcloud run deploy "${CLOUD_RUN_SERVICE}" \
  --project "${GCP_PROJECT_ID}" \
  --source "${ROOT_DIR}" \
  --region "${GCP_REGION}" \
  --no-allow-unauthenticated \
  --service-account "${RUNTIME_SERVICE_ACCOUNT}" \
  --port 8080 \
  --cpu 1 \
  --memory 512Mi \
  --concurrency 2 \
  --timeout 60 \
  --min 0 \
  --max 1 \
  --cpu-throttling \
  --set-env-vars "${ENV_VARS}" \
  --quiet

URL="$(gcloud run services describe "${CLOUD_RUN_SERVICE}" \
  --project "${GCP_PROJECT_ID}" \
  --region "${GCP_REGION}" \
  --format='value(status.url)')"

TOKEN="$(gcloud auth print-identity-token)"
curl --fail --silent --show-error \
  -H "Authorization: Bearer ${TOKEN}" \
  "${URL}/health"

printf '\nPrivate orchestrator URL: %s\n' "${URL}"
echo "No Gemini request was made by this deployment script."
