# Operations guide

## Local development

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn tripchord.main:app --reload
npm ci
npm run dev
```

Authentication is optional in the local profile. SQLite and an in-process rate
limit are used unless PostgreSQL/Redis URLs are configured.

The safe model default is disabled. For a required-model run, configure a
supported endpoint explicitly. The 2026-08-03 DeepSeek JSON is a historical
one-off record; it predates the reproducible runner and cannot be regenerated
byte-for-byte from that artifact alone:

```bash
export MODEL_PROVIDER=openai_compatible
export MODEL_BASE_URL=https://api.deepseek.com
export MODEL_NAME=deepseek-v4-flash
export MODEL_API_KEY='<read from your secret manager>'
export MODEL_AGENTS_REQUIRED=true
export MODEL_MAX_ATTEMPTS=1
```

To produce a new auditable gateway/tool-loop smoke, explicitly acknowledge the
bounded paid call and keep the key in the environment. Without
`--ack-live-cost`, provider/model/base URL, output path, or the key, the runner
exits before issuing HTTP. A successful run makes exactly three logical model
requests and writes hashes/traces/usage—not key or prompt plaintext:

```bash
uv run python scripts/run_model_runtime_smoke.py \
  --ack-live-cost \
  --provider openai_compatible \
  --base-url https://api.deepseek.com \
  --model deepseek-v4-flash \
  --output benchmarks/results/model-runtime-smoke-new.json
```

The runner has a no-network mock contract test. A paid 2026-08-04
`deepseek-v4-flash` run passed its fixed three-request JSON/tool-loop contract;
see `benchmarks/results/model-runtime-smoke-deepseek-v4-flash-2026-08-04.json`.
The separate required-model Chrome canary reached 10 model stages but ended in
`HUMAN_BLOCK`; do not convert the gateway smoke or that fail-closed live run into
a successful full OTA E2E claim.

`GET /api/v1/agents/runtime` reports the effective provider/model, whether
models are required, the context/RAG/memory backend, the Agent/deterministic
authority split, and the extra Chrome requirements. Never infer model use from
the presence of Agent classes; inspect the per-stage trace and logical/HTTP
attempt counters.

## Local Chrome live-search profile

The Chrome Companion and live browser bridge must run on the same host. Start
the API with the loopback-only launcher:

```bash
uv run python scripts/start_live_api.py
```

The launcher creates `.runtime/browser-bridge-token` with file mode `0600` on
the first run and securely reuses it on later runs; it never prints the secret
or deletes it when the API exits. Use
`pbcopy < '.runtime/browser-bridge-token'` only for the initial extension
pairing and keep the default bridge URL
`http://127.0.0.1:8000/browser-bridge`. API restarts then reconnect without
another paste. An existing token that is a symlink, is owned by another user,
or has permissions other than exact `0600` is rejected rather than silently
repaired. The API refuses non-loopback bridge clients, and the Web UI checks a
fresh Ctrip/Qunar/Tongcheng Companion heartbeat before submitting a live run.
An unreachable, stale, or incomplete Companion fails closed.

The bridge pairing token and control token have different jobs. The optional
control token protects only the external loopback HTTP reconcile endpoint; it
is not required by the internal Runtime Supervisor and must never be copied to
the extension or an LLM context. The standard launcher explicitly enables that
internal supervisor with
`TRIPCHORD_BROWSER_COMPANION_AUTO_RELOAD_ENABLED=true`, so verified source
changes can be reconciled without another manual extension reload.

Every Browser Companion release must pass the default read-only gate:

```bash
uv run python scripts/browser_companion_release_gate.py
```

It fails before tests when `src/build-meta.js` is stale or the owner-only
`.tripchord-release-seal.json` does not exactly bind the manifest version,
runtime version, fixed-manifest build SHA and build-meta SHA. It then runs the
Companion JavaScript contracts and targeted API control/launcher tests. It does
not update versions, build metadata or the seal. Only after a release author
has explicitly changed the versioned source may metadata regeneration be
requested:

```bash
uv run python scripts/browser_companion_release_gate.py --update-build-meta
```

This explicit mode durably invalidates the old seal before updating
`src/build-meta.js`, tests the candidate while it remains unavailable to the
automatic-reload supervisor, and atomically commits a mode-`0600` seal only
after all contracts pass and the candidate identity is unchanged. Caught
failures restore the previous metadata/seal; a process crash leaves the build
unsealed and therefore non-publishable. The gate never increments
`manifest.json` or runtime versions, and the Runtime Agent cannot select or
write the seal through its model-facing tool schema.

Browser-task recovery is optional. Set an absolute local-only path before the
launcher when restart recovery is needed:

```bash
export TRIPCHORD_BROWSER_BRIDGE_STATE_PATH="$PWD/.runtime/browser-bridge-state.json"
uv run python scripts/start_live_api.py
```

The `0600` atomic state file excludes pairing tokens, lease tokens and browser
heartbeat identity. It can contain request fields, sanitized visible evidence,
provider URLs and failure diagnostics, so it must be treated as private travel
data. Terminal records older than one hour are pruned on startup and queue
activity, and terminal state is capped at 256 records. This JSON
adapter is single-writer and local-process recovery only; it is not a shared or
high-availability queue. Claimed work is requeued with a new lease after
restart. The separate live-plan `run_id` cache uses a fixed TTL and, by default,
an atomic checksummed snapshot at `.runtime/live-run-cache.json`; it can survive
one API-process restart without extending the original expiry. It remains a
single-process/single-writer adapter and must not be shared by multiple workers.

After an accepted live package exists, the UI can explicitly start opt-in
periodic read-only revalidation. The backend rechecks one current component per
cycle and first sends the semantic diff through one model Event Diagnoser. A
bounded local path then uses deterministic Repair, the main Verifier, the
heterogeneous deterministic ReVerifier and the event safety gate; it does not
pretend to run model ReCritic or model Orchestrator stages. If the diagnoser is
allowed to escalate the event to a global replan, the system disables recent
quote reuse and reruns the full normal planning pipeline, including its model
Critic/Repair/ReCritic/Orchestrator stages.
the Event Diagnoser and any nested global pipeline share one request-wide
96-Agent ledger. Before global browser fan-out, the service preflights the
worst-case 18-Agent event plan (`E=true`, `R=false`, `C=256`) and returns a
structured `HUMAN_BLOCK` without starting the global search when capacity is
insufficient. Monitor lifecycle is process-local:
restarting the API stops the monitor even though the underlying live run may be
restored. It is polling, not a provider webhook, inventory lock or booking job.

## Deployment profile

Generate an opaque token map outside the repository:

```bash
python3 -c 'import json,secrets; print(json.dumps({secrets.token_urlsafe(32): "demo-tenant"}))'
export TRIPCHORD_AUTH_TOKENS='<paste-json-output>'
docker compose up --build
```

The Compose profile enables mandatory authentication and starts PostgreSQL,
Redis, the migrated FastAPI service, and Nginx/React. Enter the same opaque token
in the Web UI; it is stored only in browser `sessionStorage`.

Compose intentionally does **not** enable the browser bridge. A container does
not own the host user's Chrome profile, logged-in tabs, extension storage or
loopback namespace. Nginx routes `/browser-bridge/` to the API so requests get a
real disabled/unauthorized response rather than the React SPA fallback, but the
Compose profile is for replay/API deployment—not host-Chrome automation. Run
the loopback API launcher directly on the Chrome host for live browser search;
do not mount a personal Chrome profile into the container.

Static tokens are appropriate for a portfolio deployment or a controlled
internal pilot. An internet-facing multi-user service should terminate OIDC at
an API gateway and map verified identity claims to the existing `tenant_id`
boundary.

## Health and telemetry

- `GET /health`: process liveness.
- `GET /ready`: database connectivity and active rate-limit backend.
- `GET /metrics`: Prometheus text for request counts/duration and planning-job
  outcomes.
- Every response carries `X-Request-ID`; access logs are single-line JSON and do
  not include credentials or request bodies.
- Planning jobs expose a persisted `trace_id`, attempt count, lease expiry,
  stage, progress, result, and bounded error message.

## Persisted workspace-job recovery

The database-backed workspace planning requests accept `Idempotency-Key`. Repeating the same key and payload
returns the original workspace/job; reusing the key with different data returns
409. A worker atomically claims queued or expired-lease work, increments the
attempt, and refreshes its lease at stage transitions. Startup recovery enqueues
persisted work again, while the database claim prevents a healthy job from being
executed twice.

This is separate from `POST /api/v1/agents/live-flexible-plan-from-text/jobs`.
The latter is the cancellable browser-search control plane: it also supports a
tenant-scoped `Idempotency-Key`, but its job registry and idempotency map are
process-local. A restart loses those job records; it must not be described as
database-backed recovery or a durable production queue.

## Data-source checklist

1. Configure only authorised provider credentials.
2. Verify the readiness matrix in `docs/providers.md`.
3. Keep replay, sandbox, user snapshot, live search, revalidated, and booked
   labels separate in UI and logs.
4. Revalidate a selected bookable offer immediately before confirmation.
5. Send rail users to the official channel; do not add an undocumented 12306
   scraper.
6. Keep the production Chrome host permission set limited to Ctrip, Qunar and
   Tongcheng (`*.ctrip.com`, `*.qunar.com`, `*.ly.com`, `*.elong.com`).
7. Treat `tripchord-visible-dom-v3` as a versioned evidence contract. On
   `dom_drift`, inspect bounded sanitized diagnostics, update fixtures and
   contract tests, and bump the parser version instead of guessing a price.

## Backup and incident response

- Back up the PostgreSQL volume before schema changes and test restore against
  the current Alembic head.
- If jobs stop advancing, inspect `/ready`, then the job `trace_id`, lease,
  attempt count, and JSON access logs. Restarting the API safely reclaims queued
  or expired work.
- If Redis is unavailable, the deployment readiness should be treated as
  degraded; the local in-memory limiter is not a cross-instance substitute.
- If a provider degrades, retain healthy-provider partial results and keep the
  structured timeout/failure rather than silently relabelling cached data live.
