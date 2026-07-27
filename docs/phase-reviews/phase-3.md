# Phase 3 review — Planner–Verifier–Repair

Status: passed

## Planned

- Separate draft guidance from confirmation-time hard gates.
- Verify required visits, travel gaps, offer freshness, and source traceability.
- Repair deterministic timing and budget failures without asking an LLM to judge
  arithmetic or fabricate missing inventory.
- Bound repair iterations and return a versioned, auditable diff.

## Actual

- Added explicit draft and confirmation verification modes. A stale offer is a
  draft warning but a confirmation error.
- Added deterministic checks for must-visit coverage, sourced travel gaps,
  referenced-offer availability, provenance, dates, windows, overlap, budget,
  and currency.
- Added a repair engine that shifts unlocked items for overlap/travel gaps,
  moves activities inside daily windows, removes the lowest-utility optional
  items to satisfy a known budget, and removes unlocked out-of-range items.
- Missing sources, stale confirmation prices, missing required candidates, and
  currency conflicts remain explicitly unresolved; repair does not invent data.
- Added a bounded workflow with `ready`, `blocked`, and `exhausted` outcomes,
  parent-linked plan versions, per-iteration actions, and item-level diffs.
- Exposed verification context and repair through the API.

## Verification

- Python tests: 29 passed.
- Frozen repair scenarios: 4/4 passed.
- Existing optimiser scenarios: 3/3 passed.
- Ruff: passed.
- mypy strict: passed.
- React unit test and production build: passed.

## Deviations and findings

- The initial verifier only checked the generated plan in isolation. It could
  not distinguish a usable draft from a confirmable itinerary. Verification
  context now carries offer state, mode, and sourced travel-time requirements.
- A generic LLM repair agent was rejected for hard failures: it could produce a
  plausible but untraceable train or hotel. Deterministic repairs now cover only
  transformations with enough evidence; everything else blocks for new data.
- Warnings do not prevent a draft from being shown, while confirmation errors
  do. This keeps exploration usable without weakening the booking gate.

## Decision

Pass. The planning loop now has a reproducible failure-and-repair contract.
The next phase will inject real-world events and prove that local replanning
changes only the affected subgraph while preserving locked and unaffected items.
