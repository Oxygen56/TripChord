# TripChord evaluation lab

Frozen replay scenarios are immutable once a phase result has been reported.
New failures are added as new scenarios; old expected outputs are changed only
with a documented benchmark correction.

Live canaries are stored separately because supplier inventory, weather, and
prices are volatile. Live results must never be compared with replay results as
if they came from the same distribution.

The historical live-v3 capability-matrix Done-Gate passed on 2026-08-03. Its machine bundle
is `results/live-flight-only-final-done-gate-2026-08-03.json`; the adjacent
Markdown file records the exact scope, hashes, budget boundary, and interview-safe
claims. This is one authorized Chrome-session result, not a production SLA and
not evidence that the current strict live-v4 gate has passed.

The latest sealed current-contract bundle is
`results/live-done-gate-v4-round17-async-v13.json` (mode `0600`). Its async job
reached `succeeded/complete`, recorded three bound checkpoints and a job-scoped
47/47 successful DeepSeek trace receipt, but the runner exited with
`run_status=done_gate_failed`. The middle pair was isolated after an Evidence
Arbiter proposal-policy conflict; the other two pairs were non-recommendable
because only one provider supplied an exact lodging price. The policy conflict
was subsequently fixed and completed without the former policy conflict in a
same-date focused live run (23/23 model calls), while the two-provider lodging
gate remained blocked.
This diagnostic focused run is not a replacement sealed Done-Gate bundle.
The user explicitly removed Tongcheng overseas lodging from the active scope on
2026-08-05 after its single-source canary returned `login_required`. The source
is not retried or counted as coverage; this scope decision does not convert the
failing two-provider lodging gate into a pass.

The final Companion `0.1.16` focused browser canary has two deliberately
separate files. `results/live-browser-lodging-focused-v16-2026-08-05.json` is
the compact summary derived from the primary evidence. The mode-`0600`
`results/live-browser-lodging-focused-v16-2026-08-05.sealed.json` is the
recomputable evidence artifact: it contains all 18 redacted Ctrip quote
projections, the complete bounded Qunar login-navigation diagnostic, the
token-free applied reload receipt, query/build/runtime bindings, source-artifact
hashes, and canonical input/result SHA-256 digests. Raw visible page text,
credentials, cookies and tracking-query values are excluded. Its release binding
also confirms that the captured task runtime, build-meta, release seal and current
fixed source manifest all resolve to the exact same `0.1.16` build SHA. Verify
its internal projections and hashes
without contacting Chrome, the bridge or a model:

```bash
uv run python scripts/capture_live_browser_evidence.py \
  --verify benchmarks/results/live-browser-lodging-focused-v16-2026-08-05.sealed.json
```

This makes the one-provider result auditable; it does not turn the still-failing
1/2 lodging coverage or unattempted Publication Gate into a pass.

`run_live_done_gate_v4.py` is an additive, fail-closed live gate for the
pre-frozen stay-plan candidate set. It does not rewrite the live-v3 scenario or
evidence bundle. The v4 gate binds the scenario SHA before search, requires
13 browser Source tasks per date pair, preserves four distinct lodging inventory
states (`quote_found`, `confirmed_empty`, `bounded_no_exact_quote`, and
`bounded_provider_pending`), and requires the scenario-frozen two-provider exact
quote threshold for every selected stay segment. Only when a recommendable,
published option exists does it inject an explicitly synthetic `sold_out` fault
with source `tripchord-synthetic-done-gate-fault-injection`, then performs a real
read-only same-provider page requery, and checks the
Planner–Verifier–Repair–ReVerifier–master stay-plan handoff before reporting a
recommendation. The three date pairs use batch admission over the Companion's
six global read-only leases so all 39 browser tasks receive a viable execution
window. The frozen server execution budget is 3600 seconds. The runner submits
only after runtime preflight reports an effective flexible timeout of exactly
3600 seconds. It submits
that work through `POST /api/v1/agents/live-flexible-plan-from-text/jobs`, derives
its idempotency key from the canonical API-payload SHA plus one fresh per-run
attempt ID, and polls the returned job with the same tenant-scoped authenticated
client. The scenario SHA and API-payload SHA remain separate evidence identities.
Every snapshot must bind the latter; checkpoint revisions may only extend an
immutable ordered prefix, and a successful run must expose exactly three
checkpoints aligned with the final pair-run dates, states, and query-task IDs.
Its evidence records the job ID, replay flag, bound status URL, observed revisions,
stage/progress changes, whitelisted terminal metadata plus the result digest, and
the job-bound model trace receipt. The complete terminal result is not duplicated
under `terminal_job`; only separately validated result projections enter the bundle.
The process-global model counter is diagnostic only. A failed or cancelled
job, a tenant-scoped 404, or expiry of the 3900-second default client wait budget
fails closed before the Done-Gate and triggers a best-effort same-tenant `DELETE`
whose whitelisted receipt is persisted. API and Browser Bridge credentials are
neither inputs to the idempotency key nor persisted in the evidence bundle; nested
sensitive fields and URL query credentials are recursively redacted, and the bundle
is atomically replaced with mode `0600`. If the strict live run has
no recommendable published option, the runner still emits the complete failing
Done-Gate report with `run_status=done_gate_failed` and records
`skipped_reason=no_recommendable_published_option`; it does not inject the
synthetic event. The injected fault is not evidence that the platform reported
a sold-out room (and it does not claim a natural price change); a passing run
proves only that a stable, different, currently available product was found and
accepted as
a one-remove/one-add repair after the hypothetical target was excluded.

Run the current deterministic verifier baseline:

```bash
uv run python benchmarks/evaluate.py
uv run python benchmarks/evaluate_planning.py
uv run python benchmarks/evaluate_repair.py
uv run python benchmarks/evaluate_events.py
uv run python -m benchmarks.evaluate_scale
uv run python -m benchmarks.evaluate_replanning_scale
uv run python -m benchmarks.evaluate_faults
uv run python -m benchmarks.evaluate_date_search \
  --output benchmarks/results/date-search-full-universe-v1.json
uv run python -m benchmarks.evaluate_agents \
  --output benchmarks/results/agent-suite-v1.json
uv run python -m benchmarks.evaluate_agent_architectures \
  --output benchmarks/results/agent-architecture-v1.json
uv run python -m benchmarks.evaluate_package_candidate_diversity \
  --output benchmarks/results/package-candidate-diversity-v1.json
```

The full-August date-search benchmark enumerates all 124 synthetic date pairs but
limits exact-query selection to budgets 3/5/8. The selector cannot see the exact
oracle before choosing; the oracle is evaluation-only. Its frozen result currently
shows that the adaptive selector loses to the simple coarse-price Top-K baseline in
aggregate, so it explicitly forbids an adaptive-winner or real-OTA-quality claim.
See `docs/date-search-benchmark.md`.

The follow-up `coverage-guarded-hybrid-v2` was calibrated without reading the
separate test split, then evaluated once on a policy-hash-derived 4–7-night sealed
holdout matching the user's 5–8-calendar-day contract. It failed the pre-frozen
acceptance gate at budget 5 and therefore remains benchmark-only: it was not added
to the planning layer. Separately, because the old adaptive default already lost
the aggregate v1 benchmark, live acquisition now conservatively consumes the
Query-Strategist-validated order as bounded Top-K; adaptive remains injectable and
experimental. This is not a real-OTA superiority claim. See
`docs/date-search-hybrid-v2.md` and run:

```bash
uv run python -m benchmarks.evaluate_date_search_hybrid \
  --output benchmarks/results/date-search-hybrid-v2.json
```

`planning-scale-v1.jsonl` is generated once from seed `20260727` and checked in.
Regenerate it only when creating a new benchmark version:

```bash
uv run python benchmarks/generate_scale_scenarios.py
```

Scale metrics are synthetic replay measurements. They establish deterministic
constraint and recovery behaviour, not production traffic, preference quality,
or supplier-network latency.

`package-candidate-diversity-v1.json` freezes one synthetic normalized-quote
regression for `package-candidate-beam-v3`: three synthetic flight providers,
three flight identities, two feasible package kinds, and `candidate_cap=3`.
It verifies that the globally best candidate survives, the bounded pool retains
all three providers/flights and both kinds, reversed input order is stable, and
the generated pool stays inside the audited structural bound. This is a
regression claim for that fixed fixture only; it is not live OTA, bookability,
platform-superiority, exhaustive-search, recall, or production-SLA evidence.

`agent-suite-v1.jsonl` adds 240 balanced tasks across 12 categories. Its evaluator
compares a deterministic baseline, a single-candidate deterministic proxy, and
the complete multi-agent path, then checks constraint violations, explicit
preference overrides, evidence traceability, recovery, L3 authorization, loops,
and parallel-vs-serial latency. The 75% proxy result is not a one-shot LLM Agent
result. All model and travel-inventory outputs in that suite are frozen replay fixtures.
The latency comparison declares a fixed 5ms delay for each scripted model call;
the value is persisted in the result JSON. This keeps interpreter overhead from
dominating the measurement and is not presented as supplier latency or production QPS.

The architecture evaluator is a separate, fair single-LLM-Agent versus multi-Agent
harness. Both arms receive the same frozen tasks, exact tool contract, model identity,
temperature, and total call/token budgets. Its default scripted policy validates the
harness only and deliberately emits `winner_claim_allowed=false`; it does not turn the
old single-candidate proxy into an LLM baseline. See
`docs/benchmark-agent-architectures.md` for metric definitions and the explicitly
cost-gated live-model pilot command.
