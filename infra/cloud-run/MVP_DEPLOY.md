# Cloud Run MVP deployment

Target Google Cloud project: `physical-ai-guardian`.

The first deployment is deliberately CPU-only and deterministic. It gives the hackathon a real Google Cloud Run URL before GPU/model artifacts are ready.

## Fastest manual deployment

Prerequisites: Google Cloud CLI authenticated to an account that can deploy to `physical-ai-guardian`, with billing enabled.

```bash
gcloud auth login
gcloud config set project physical-ai-guardian
./scripts/deploy_cloud_run_mvp.sh
```

The deploy script defaults to:

- project: `physical-ai-guardian`
- region: `asia-northeast1` (Tokyo)
- service: `disaster-agentic-mvp`

Override with `GCP_PROJECT_ID`, `GCP_REGION`, or `CLOUD_RUN_SERVICE` only when intentionally deploying elsewhere.

The script enables Cloud Run, Cloud Build and Artifact Registry APIs, then uses `gcloud run deploy --source .`. Cloud Build builds the image and Cloud Run deploys it.

Cost controls in the service configuration:

- CPU only
- 1 vCPU / 512 MiB
- minimum instances 0
- maximum instances 1
- concurrency 4
- 60 second request timeout (a live Gemini run waits for the private service)
- CPU throttled outside requests; startup CPU boost disabled
- custom /v1/simulate API disabled by default on public deployment
- runs as `evacweave-public-sa`, which has no project role and no Vertex AI permission; with `LIVE_AI_ENABLED=1` it may read and write objects in one private bucket only (`roles/storage.objectUser` on `physical-ai-guardian-evacweave-live-quota`, created by `scripts/setup_live_quota.sh`), where the daily live-Gemini count is kept so a new instance does not reset it
- no model call from this service; live Gemini runs and report readings (`LIVE_AI_ENABLED=1`, off by default) go through the private service, one at a time, `LIVE_AI_DAILY_RUNS` per day in total

To turn live Gemini runs on, deploy the private service first and then:

```bash
./scripts/setup_gemini_iam.sh            # once: service accounts and the custom role
./scripts/deploy_cloud_run_orchestrator.sh
LIVE_AI_ENABLED=1 ./scripts/deploy_cloud_run_mvp.sh
```

`scripts/release_with_gemini.sh` runs these steps plus the paid record and live checks, asking before each paid call.

These guards do not create a fixed billing ceiling. See [費用とアクセス制限](COST_GUARD.md) for emergency shutdown and Cloud Billing spend caps.

## Source-deploy IAM

Google's current Cloud Run source-deploy documentation requires the deployer to have the equivalent of Cloud Run Source Developer and Service Usage Consumer, plus Service Account User on the service identity. The build service account also needs the Cloud Run Builder role.

If a source deploy fails with an IAM error, inspect the exact principal in the error before granting anything; keep the roles scoped to this project.

## GitHub Actions deployment

`.github/workflows/deploy-cloud-run.yml` is manual-only. The project ID is fixed to `physical-ai-guardian`. It uses GitHub OIDC + Google Workload Identity Federation instead of a long-lived JSON service-account key.

Optional repository variables:

- `GCP_REGION` (defaults to `asia-northeast1`)
- `CLOUD_RUN_SERVICE` (defaults to `disaster-agentic-mvp`)

Repository secrets still required for GitHub Actions deployment:

- `GCP_WIF_PROVIDER` — full Workload Identity Provider resource name
- `GCP_DEPLOY_SERVICE_ACCOUNT` — deployment service-account email

Do not store service-account JSON keys in the repository.

## Smoke test

`deploy_cloud_run_mvp.sh` checks `/health`, the showcase contents (`evacweave-showcase/v2`, the agent outcome, the river-district run), `/lab`, `/api-guide` and slide 9 right after deploying. If a check fails it prints the exact `gcloud run services update-traffic … --to-revisions=<previous>=100` command that serves the previous revision again. The commands below repeat the basic checks by hand.

```bash
URL="$(gcloud run services describe disaster-agentic-mvp   --project physical-ai-guardian   --region asia-northeast1   --format='value(status.url)')"

curl -fsS "$URL/health"
curl -fsS "$URL/v1/demo/blocked"
curl -fsS "$URL/v1/demo/repaired"
```

Expected golden statuses are `FAIL` and `PASS` respectively. Those statuses apply only to the included synthetic scenario.
