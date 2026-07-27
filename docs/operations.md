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

## Job recovery

Planning requests accept `Idempotency-Key`. Repeating the same key and payload
returns the original workspace/job; reusing the key with different data returns
409. A worker atomically claims queued or expired-lease work, increments the
attempt, and refreshes its lease at stage transitions. Startup recovery enqueues
persisted work again, while the database claim prevents a healthy job from being
executed twice.

## Data-source checklist

1. Configure only authorised provider credentials.
2. Verify the readiness matrix in `docs/providers.md`.
3. Keep replay, sandbox, user snapshot, live search, revalidated, and booked
   labels separate in UI and logs.
4. Revalidate a selected bookable offer immediately before confirmation.
5. Send rail users to the official channel; do not add an undocumented 12306
   scraper.

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
