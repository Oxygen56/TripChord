# Phase 2 review — requirements, candidates, and deterministic scheduling

Status: passed

## Planned

- Parse common Chinese free-travel requests into typed constraints.
- Ask for missing hard fields instead of inventing them.
- Convert sourced places into scored activity candidates.
- Produce a deterministic schedule that respects dates, daily windows, opening
  windows, visit duration, travel gaps, daily activity caps, must-visit items,
  and known CNY activity budget.

## Actual

- Added an evidence-producing Chinese fallback parser with explicit missing
  fields and clarification questions.
- Extended `TripSpec` with daily main-activity limits.
- Added preference scoring, avoid filtering, must-visit promotion, opening-hour
  intersection, duration policy, and source propagation.
- Added an OR-Tools CP-SAT optimiser with deterministic single-worker settings,
  optional activity intervals, multi-window availability, pairwise travel time,
  budget, and daily-count constraints.
- Added conversion from solver output to timezone-aware `PlanVersion` items.
- Added trip-parse and plan-optimisation API endpoints.
- Added three frozen optimiser scenarios covering must-visit plus budget,
  infeasible closure, and multi-day daily caps.

## Verification

- Python tests: 22 passed.
- Frozen optimiser scenarios: 3/3 passed.
- Ruff: passed.
- mypy strict: passed.
- Every solver run uses one worker and a fixed seed for replay stability.

## Deviations and findings

- Opening hours can contain multiple windows in one day. The first optimiser
  draft collapsed them to one; review caught this and the model was changed to
  select exactly one compatible window.
- The deterministic fallback parser covers common Chinese request shapes but is
  not presented as a general natural-language solution. An LLM extractor must
  return the same evidence-bearing draft and cannot bypass missing-field gates.
- The current budget constraint covers activity costs inside this solver.
  Transport and lodging become locked cost anchors when the end-to-end Planner
  assembles the full problem.
- Route times are accepted as a sourced matrix; matrix acquisition and cache
  policy remain in the orchestration phase.

## Decision

Pass. The hard-constraint scheduling kernel is ready to be wrapped by the
Planner–Verifier–Repair loop. Preference quality remains an evaluation target,
not an established result.

