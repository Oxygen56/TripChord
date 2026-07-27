# Phase 6 review — scale benchmark, ablations, and fault injection

Status: passed

## Planned

- Replace anecdotal demo claims with a frozen, seeded scenario set.
- Compare the constraint optimiser with a deterministic greedy planner.
- Remove travel and budget mechanisms separately to measure why they matter.
- Compare event-scoped repair with full regeneration on preservation and utility.
- Inject provider failures and deadlines to verify partial-result isolation.

## Actual

- Added 120 checked-in planning scenarios generated with seed `20260727`.
  Each contains 2–3 days, ten candidates, one required visit, multiple windows,
  pairwise travel times, costs, utilities, a daily cap, and a budget.
- Added an earliest-fit greedy baseline and an evaluator that independently
  checks must-visits, uniqueness, budget, duration, availability, daily caps,
  and travel gaps.
- Added no-route and no-budget ablations against the original full constraints.
- Added 120 matched closure-event comparisons between local replanning and full
  global regeneration.
- Added concurrent injected failure and timeout providers. The provider registry
  now has a per-provider deadline and returns structured retryable timeouts while
  retaining healthy-provider results.

## Results

- Full CP-SAT hard-constraint validity: 120/120 (100%).
- Greedy hard-constraint validity: 120/120; CP-SAT mean utility was 1249.58
  versus 1239.36, a measured 0.83% lift on this set.
- Removing travel constraints: 0/120 valid under the original route matrix.
- Removing budget constraints: 37/120 valid (30.83%) under the original budget.
- Two deterministic reruns produced the same schedule SHA-256:
  `2ed88466112e0f4bd701e9ba5e72d5c03271756f628378c7ae664501dbd75b91`.
- CP-SAT latency across the two local runs: p50 40.50–47.61 ms and p95
  121.58–136.57 ms.
- Local closure recovery: 120/120 (100%); unaffected-item preservation: 100%.
- Mean whole-plan preservation was 83.38% for local replanning versus 17.28%
  for full regeneration.
- This stability has a measured trade-off: mean utility retention was 82.30%
  locally versus 91.66% after full regeneration.
- Across 100 concurrent replay queries with one failing and one hanging provider,
  every query retained the healthy result and classified both failures,
  including the timeout as retryable.

## Deviations and findings

- “Greedy is invalid” was not supported: the implemented baseline remained
  feasible. The defensible result is a modest utility lift, not a large validity
  claim.
- Local repair preserves user intent far better than global regeneration but
  may leave capacity unfilled. The product should expose a future choice between
  “minimum change” and “re-optimise quality” rather than pretending one policy
  dominates both objectives.
- Provider throughput in the fault test uses in-memory replay and injected
  providers. It is intentionally excluded from network or production claims.

## Decision

Pass. The planning, ablation, recovery, latency, determinism, and provider-fault
claims now have reproducible evidence. The next phase will build post-training
data from these traces and require held-out improvement before claiming that a
fine-tuned model adds value beyond deterministic planning.
