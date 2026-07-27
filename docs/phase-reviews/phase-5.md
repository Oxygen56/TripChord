# Phase 5 review — persistent full-stack planning workspace

Status: passed

## Planned

- Persist trip requirements, plan lineages, events, and task state.
- Hide solver internals behind a user-facing end-to-end planning request.
- Stream long-running task progress without holding a blocking HTTP request.
- Show price truth labels, daily timeline, plan-version diffs, and event
  injection in a responsive React workspace.
- Provide reproducible local and PostgreSQL-backed deployment paths.

## Actual

- Added SQLAlchemy async persistence for workspaces, immutable sequential plan
  versions, idempotent events, and durable job state. SQLite is the local
  default and the same models target PostgreSQL through `asyncpg`.
- Added an Alembic initial migration and drift check.
- Added a persistent planning runner with queued, optimizing, verifying,
  complete, and failed states. The API streams state transitions over SSE.
- Added a replay place catalog and server-side problem assembler. Route times in
  this path are explicitly labelled synthetic replay estimates.
- Added one-call trip planning, workspace CRUD, version comparison, persisted
  event replan, job polling, and job stream endpoints.
- Replaced the static frontend mock with a working planning form, live progress,
  replay offer cards, per-day timeline, version selector, item-level diff, and
  event injection lab.
- Added API and web container images, Nginx API/SSE proxying, PostgreSQL Compose
  topology, health checks, and database migration on API startup.

## Verification

- Python tests: 47 passed, including database, job, SSE, assembler, workspace,
  and persisted-replan integration tests.
- Ruff and mypy strict: passed.
- React test and TypeScript/Vite production build: passed.
- Alembic upgrade succeeded and `alembic check` reported no schema drift.
- `docker compose config`: passed.
- Browser flow passed: submitted a two-day Beijing request, received a ready v1,
  inspected two replay offers, injected a museum-closure event, and received v2
  with one removed item and 100% unaffected-item preservation.
- Browser console warnings/errors during that flow: 0.

## Deviations and findings

- An API that accepted a raw `PlanningProblem` was not a complete product path.
  A server-side assembler now converts the user's `TripSpec` into candidates and
  a travel matrix before creating the job.
- A published event plan could skip from v1 to v3 when an internal repair
  iteration created another draft. Internal iterations remain in the workflow
  trace, while the persisted result is normalised to the next public version.
- ORM lazy relationship access caused an async `MissingGreenlet` failure in the
  first repository test. All snapshots now load relationships explicitly before
  leaving the awaited query boundary.
- Background execution is persistent in state but currently in-process. A
  worker crash can leave a running job for the reliability phase to reclaim;
  this phase does not claim distributed queue durability.

## Decision

Pass. The project is now an operable full-stack planning product rather than a
static Agent demo. Next, the evaluation phase will scale frozen scenarios,
measure baselines/ablations/latency, and test injected provider and worker
failures before any quality or reliability claim is written.
