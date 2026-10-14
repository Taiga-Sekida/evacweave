#!/usr/bin/env bash
# Least-privilege identities for Gemini on Vertex AI. Idempotent: safe to re-run.
#
#   guardian-agent-sa    runtime of the private agent service. Its only project role is
#                        the custom role guardianVertexGenerate (aiplatform.endpoints.predict,
#                        the permission behind Vertex AI generateContent).
#   evacweave-public-sa  runtime of the public service. No project role at all; the public
#                        deploy grants it run.invoker on the private service only.
#
# No keys are created. Makes no model call.
set -euo pipefail

PROJECT="physical-ai-guardian"
if [[ "${GCP_PROJECT_ID:-${PROJECT}}" != "${PROJECT}" ]]; then
  echo "Refusing to change IAM in unexpected project: ${GCP_PROJECT_ID}" >&2
  exit 2
fi
AGENT_SA="guardian-agent-sa@${PROJECT}.iam.gserviceaccount.com"
PUBLIC_SA="evacweave-public-sa@${PROJECT}.iam.gserviceaccount.com"
ROLE="guardianVertexGenerate"
PERMISSION="aiplatform.endpoints.predict"

gcloud services enable iam.googleapis.com aiplatform.googleapis.com --project "${PROJECT}"

ensure_service_account() {
  local email="$1" display="$2"
  if gcloud iam service-accounts describe "${email}" --project "${PROJECT}" >/dev/null 2>&1; then
    echo "service account exists: ${email}"
  else
    gcloud iam service-accounts create "${email%%@*}" --project "${PROJECT}" --display-name "${display}"
  fi
}
ensure_service_account "${AGENT_SA}" "EvacWeave private agent (Gemini on Vertex AI)"
ensure_service_account "${PUBLIC_SA}" "EvacWeave public web (no Vertex AI access)"

# A deleted custom role keeps its id for a while; bring it back instead of failing.
STATE="$(gcloud iam roles describe "${ROLE}" --project "${PROJECT}" --format='value(deleted)' 2>/dev/null || echo missing)"
if [[ "${STATE}" == "missing" ]]; then
  gcloud iam roles create "${ROLE}" --project "${PROJECT}" \
    --title "Guardian Vertex generate" \
    --description "Call Gemini generateContent on Vertex AI, nothing else" \
    --permissions "${PERMISSION}" --stage GA
elif [[ "${STATE}" == "True" ]]; then
  gcloud iam roles undelete "${ROLE}" --project "${PROJECT}"
fi
if [[ "$(gcloud iam roles describe "${ROLE}" --project "${PROJECT}" --format='value(includedPermissions)')" != *"${PERMISSION}"* ]]; then
  gcloud iam roles update "${ROLE}" --project "${PROJECT}" --add-permissions "${PERMISSION}" --quiet
fi

gcloud projects add-iam-policy-binding "${PROJECT}" \
  --member "serviceAccount:${AGENT_SA}" \
  --role "projects/${PROJECT}/roles/${ROLE}" \
  --condition None --quiet >/dev/null
echo "granted ${ROLE} to ${AGENT_SA}"
echo "${PUBLIC_SA} has no project role; run.invoker on the private service is granted at public deploy."
echo "IAM changes can take a few minutes to apply. No Gemini request was made by this script."
