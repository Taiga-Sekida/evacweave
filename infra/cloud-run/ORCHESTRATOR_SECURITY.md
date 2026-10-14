# Private Vertex orchestrator security profile

The Vertex Gemini orchestration service is intentionally separate from the public deterministic MVP.

## Authentication and exposure

- Cloud Run is deployed with `--no-allow-unauthenticated`.
- Cloud Run IAM remains enabled.
- The service has no API key and no long-lived service-account JSON key.
- Developers test it with a short-lived `gcloud auth print-identity-token`.
- The only production caller is the public service, running as `evacweave-public-sa@physical-ai-guardian.iam.gserviceaccount.com`. That identity has no project role; it is granted `roles/run.invoker` on this service only (and, to keep the daily live count, `roles/storage.objectUser` on one private bucket). It takes a Google-signed ID token from the Cloud Run metadata server with this service's URL as the audience, sends it only to this origin and never follows redirects.
- For an agent run, the public service forwards a fixed synthetic scenario (chosen by key) with at most two of its own roads closed, and replays every returned trace through the Simulation Core before showing it (`apps/api/live_agent.py`).
- For a report reading, it forwards one line of visitor text (at most 120 characters, no control characters) with that scenario's road list. This is the only visitor text that reaches this service and the model; see "Report reading" below.

## Runtime identity and least privilege

Runtime identity:

`guardian-agent-sa@physical-ai-guardian.iam.gserviceaccount.com`

The deployment preflight requires the custom role `guardianVertexGenerate` to include `aiplatform.endpoints.predict`, the permission used by Vertex GenerateContent. The service should not receive Owner, Editor, or Vertex AI Admin. `scripts/setup_gemini_iam.sh` creates both service accounts and the role idempotently, without keys.

## Prompt/data integrity

The private endpoint `POST /v1/agent/run` accepts `Scenario + Plan`, not a caller-provided `SimulationResult`. Every verdict in the loop is computed here by the deterministic Simulation Core, so a caller cannot inject a fake PASS/FAIL into the model prompt.

Gemini is the *policy* of the guarded agent loop (`packages/agents/loop.py`). Each turn it receives one observation derived from verified state plus the menu of allowed decisions, and returns one `{action, reason_code}`. The loop, not Gemini, decides which actions are allowed: a failing plan can only be repaired or escalated, never finished. A disallowed action, invalid output, model error or exhausted call budget halts the run and is recorded in the trace (fail closed). The prompt carries only counts, flags and engine/loop codes; the caller-chosen plan id is left out, so no caller-written text reaches the model. Unexpected exceptions return 500 and are logged by type only, so a traceback cannot echo the caller's input.

Replay (`replay_matches`) proves that recorded decisions lead to the recorded results, and recounts the model calls instead of trusting the trace; it does not prove that a decision came from Gemini, because traces are not signed. For live runs, authorship rests on the IAM-authenticated channel from the public service to this one.

## Report reading (`POST /v1/report/read`)

The report is untrusted and may contain prompt injection. It is sent to Gemini as a JSON string inside the data, under a system instruction (`report_reader/v1`) that says the JSON is data, never instructions. The response schema is built per request: `closed_roads` is an array (at most two items) whose items are an enum of the road ids offered. The answer is re-validated here (`parse_reading`) and again by the public service, which also checks the ids against the scenario before closing any road. So the worst a malicious report can do is make Gemini name the wrong roads of that town (or none); the reading is shown to the visitor, and any plan for that world is still found by the loop and judged by the Simulation Core. The report text is never logged: the log entry `report_read` carries its length, the number of roads offered, the roads read and the `run_id`. One reading is one Gemini call.

## Abuse/cost controls

- private IAM-only service; the public caller is limited to one live request (a run or a reading) at a time and `LIVE_AI_DAILY_RUNS` requests per day in total (default 40), off unless `LIVE_AI_ENABLED=1`
- request body <= 128 KiB
- strict Pydantic contracts and duplicate-key rejection
- Gemini model allowlist: `gemini-3.5-flash`（既定）, `gemini-3.7-flash`, `gemini-3.8-flash`。`gemini-2.5-flash`はVertex AIで2026-10-20に退役するため含めない。コードを変えずに切り替えられるよう、Cloud Runの`VERTEX_GEMINI_MODEL`環境変数で選ぶ
- Vertex location allowlisted: `global` (default), `us-central1`, `asia-northeast1` (`GOOGLE_CLOUD_LOCATION`; switch when one endpoint's shared capacity answers HTTP 429). The data is synthetic
- at most 6 steps and 6 Gemini attempts per run, 1 per reading; a failed attempt is never refunded
- response schema: two enums only (`GeminiChoice`); output re-validated against `AgentDecision`
- `thinking_level=MINIMAL` for agent decisions (`DECISION_PROFILE`), `LOW` for readings (`GEMINI_PROFILE`); `max_output_tokens=512` (1024 for a report reading, 8192 for a care reading)
- SDK retries set to one attempt
- time budget from the moment a request arrives (Cloud Run timeout 60 s): the Gemini client is prepared first (SDK import, credentials; seconds on a cold instance) outside any call's timer; one Gemini request may take 25 s, and the SDK abandons it at the same limit (`X-Server-Timeout`); no request is sent, or waited for, after 45 s (a run out of time halts with its trace, `model_budget_exhausted`); 52 s is the backstop for the whole run (504). Deterministic tools run in a worker thread, so the deadline answers on time while a budgeted tool finishes in the background
- SDK retries are off (`HttpRetryOptions(attempts=1)`, per request): a retry would be a billable call the budget never reserved
- request profiles (`GEMINI_PROFILE`): `default` (answer schema, thinking LOW), `minimal` (answer schema, thinking MINIMAL), `json` (JSON only, with the schema stated in the system instruction instead of constraining decoding). The temperature is left at Gemini 3's default (1.0), as Google recommends. In every profile the answer is re-validated against the same strict contract (an action from the allowed menu, road ids from the offered list), so the `json` profile does not widen what a prompt injection can achieve
- every Gemini request is logged as `gemini_call` (purpose, model, `elapsed_ms`, `run_id`), and a failed one as `gemini_call_failed` with only the exception class and the HTTP status when the error carries one; a client that cannot be prepared logs `gemini_client_failed`. The SDK's error message contains the response body, so it is never logged; nor are prompts, reports, answers or tokens
- `POST /v1/diagnostics/gemini` sends at most one Gemini request (the agent's first decision, or a report reading) on an explicit `confirm_paid_call: true`, one at a time, three per day per instance, and returns the failing stage, exception class, HTTP status and the time of each stage. `scripts/diagnose_gemini.py` calls it from Cloud Shell and does not repeat a failure on the same revision and model
- repair search charges every queue push (50,000 per repair), so many parallel roads cannot make one request run for minutes
- the returned trace carries the `run_id` used on every log line of the run
- Cloud Run concurrency 2
- Cloud Run max instances 1
- min instances 0 / scale to zero

Cloud Run max instances is a cost guard, not a hard billing cap. Keep Cloud Billing budgets/quotas configured independently.

## Observability

Each step is logged to stdout as one JSON line (`severity`, `message: agent_step`, run id, step, allowed actions, decision, policy error, effect, model calls), followed by an `agent_run` summary with outcome and duration. Cloud Run forwards these as structured Cloud Logging entries; a well-formed `X-Cloud-Trace-Context` header adds `logging.googleapis.com/trace` so the run lines up with the request trace. Exceptions are logged by type only, never with payloads.

## Secrets

This service does not need an API key. Vertex uses Application Default Credentials from the Cloud Run service identity. If future integrations require secrets, store them in Secret Manager rather than plain environment variables.
