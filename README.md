# TripChord（旅弦）

TripChord is a price-aware, constraint-checked planning system for independent
leisure travel. It combines traceable travel data, deterministic scheduling,
Planner–Verifier–Repair, and event-driven local replanning.

> Status: active development. The clean-room foundation, provider data layer,
> typed requirements, deterministic scheduler, and bounded
> Planner–Verifier–Repair loop are implemented and tested. Production supplier
> credentials and end-to-end planning-quality improvements remain unverified,
> so neither is claimed yet.

## Why it exists

Most itinerary demos generate plausible prose but do not prove that locations
are open, routes are feasible, prices are fresh, or a changed booking can be
repaired without destroying the rest of the trip. TripChord makes those
properties explicit and measurable.

## Core contracts

- Every price is labelled as live search, revalidated, user snapshot, replay,
  sandbox, or booked.
- Every selected bookable offer is revalidated before confirmation.
- Hard constraints are verified deterministically.
- Repairs produce an auditable diff and preserve unaffected plan items.
- Offline replay benchmarks and volatile live canaries are reported separately.

## Repository layout

```text
apps/api/          FastAPI application and planning domain
apps/web/          React planning workspace
benchmarks/        Frozen scenarios, evaluators, and run manifests
docs/              Architecture, source policy, roadmap, and phase reviews
```

## Quick start

```bash
uv sync --all-groups
uv run uvicorn tripchord.main:app --reload

npm install
npm run dev
```

The API is served on `http://localhost:8000`; the web workspace defaults to
`http://localhost:5173`.

Without external credentials the offer API runs against explicit replay data.
Provider readiness and production-verification boundaries are recorded in
`docs/providers.md`.

## Upstream comparison boundary

The original tutorial reference is Datawhale's HelloAgents repository at
commit `6c616938c521c89bc4b2bf001bf237d259f1726b`. It is licensed CC BY-NC-SA
4.0. TripChord does not copy its source; the commit is retained only as a
reproducible comparison baseline. See `docs/upstream-baseline.md`.
