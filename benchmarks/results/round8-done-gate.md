# TripChord Done-Gate Round 8 — Layer-6 Repair & Real E2E Rerun

- **Date**: 2026-08-08
- **Command**: `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py`
- **Evidence**: `benchmarks/results/live-done-gate-v4.json` (refreshed), `benchmarks/results/product-v1-done-gate.json` (refreshed)
- **Runtime**: live API `127.0.0.1:8000` healthy; Companion paired; certified canary passed (all 6 OTA scopes).

## Layer matrix

| Layer | Result |
|---|---|
| 1_reproducibility | PASS |
| 2_replay | PASS |
| 3_clean_chrome_fixtures | PASS |
| 4_model_smoke | PASS |
| 5_real_canary | PASS |
| 6_full_e2e | FAIL (honest — platform limitations below) |

## Layer-6 gate: 15 checks itemized

| # | Check | C-74 | Round-8 | Change |
|---|---|---|---|---|
| 1 | prefrozen_stay_plan_candidate_set | PASS | PASS | — |
| 2 | v4_source_graph | PASS | PASS | — |
| 3 | stage_aware_exploration_publication_contract | FAIL | FAIL | same root cause (0 recommendable → 0 publications) |
| 4 | stay_inventory_four_state_contract | FAIL | FAIL | improved: qunar snapshot/source gap closed; per-segment provider limitation remains |
| 5 | planner_verifier_repair_master_stay_plan_chain | FAIL | **PASS** | **fixed (Fix B)** |
| 6 | recommendable_date_pair_stay_plan_options | FAIL | FAIL | 0 recommendable (cascade) |
| 7 | all_recommended_publication_closures | FAIL | FAIL | cascade |
| 8 | real_v4_browser_source_evidence | FAIL | FAIL | cascade |
| 9 | flight_search_outcome_contract | FAIL | FAIL | cascade |
| 10 | observed_cross_platform_overlap | FAIL | FAIL | cascade |
| 11 | strict_selected_plan_platform_coverage | FAIL | FAIL | cascade |
| 12 | icom_exploration_and_publication_evidence | FAIL | FAIL | cascade |
| 13 | planner_verifier_repair_orchestrator | FAIL | FAIL | cascade |
| 14 | exact_budget_and_selected_evidence | FAIL | FAIL | cascade |
| 15 | event_injection_repair_reverify_master | FAIL | FAIL | cascade |

## Fixes applied and verified in the real E2E

### Fix A — source executor no longer masks browser failures (`apps/api/src/tripchord/agents/live_system.py`)
- When a browser source task raises before returning a terminal snapshot, the executor now constructs a terminal `FAILED` `BrowserTaskSnapshot` (from the frozen submission), stores it, and includes it in the scheduler result.
- **Verified**: In Round-8 evidence, `source-qunar-lodging-hulhumale-full` now has a FAILED snapshot + Source task result in every pair (C-74 had "缺少原始浏览器 snapshot" / "缺少 Source task 结果" in pair 0). The four-state summary no longer reports those two errors.

### Fix B — gate only requires ReVerifier when Repair produced a candidate (`apps/api/src/tripchord/agents/live_done_gate_v4.py`)
- `_check_selected_plan_handoffs` now flags "Repair 输出未经过 ReVerifier" only when `repair.repaired_candidate_id is not None` and `reverification is None`.
- Rationale: the `StayPlanPlanningHandoff` model (stay_plans.py) already enforces reverification=`None` when Repair rejects (nothing to reverify) and requires reverification when a repaired candidate exists. The old check was over-strict.
- **Verified**: `planner_verifier_repair_master_stay_plan_chain` **FAIL → PASS**. Pair 2 now shows `repair.repaired_candidate_id` set with `reverification` present; pairs 0/1 show `attempted=True, repaired_candidate_id=None, reverification=None` (Repair legitimately rejected).

## Remaining failures — genuine platform-side limitations (recorded honestly, not faked)

1. **qunar Maafushi exact quotes = none (confirmed_empty)** — verified via dual-observation receipt chain (same query fingerprint, explicit DOM evidence "共 家酒店满足条件" / "很抱歉，没有找到相关的酒店" / "暂无报价"; detail-fallback to Maafushi Veli / SEASUNBEACH both `no_inventory`). Every selected plan's `maafushi-full` segment therefore has only ctrip = **1 家**, below the frozen `_STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT = 2`. This is not a parser bug; it is a platform inventory limitation.
2. **qunar Hulhumale realtime search never settles** — search page stays in "请稍等,您查询的结果正在实时搜索中..." within the bounded wait (28s observed) and the page shows "共 家酒店满足条件" / "暂无报价". When the search does eventually return a terminal DOM state, the source produces a `bounded_provider_pending` receipt and a four-state row (most sources, most pairs). When the browser lease expires before the extraction settles (qunar-hulhumale-first in all 3 pairs, qunar-hulhumale-full in pair 0, qunar-hulhumale-last in pair 2), there is no terminal DOM evidence to attach an honest receipt, so the four-state row for that (provider, segment) cannot be produced. This is a genuine search-wait/lease limitation — fabricating a receipt would violate the no-fake-evidence red line.
3. **Cascade**: 0 recommendable options → 0 publications → checks 3, 6–15 fail (final_decision = `human_block`, "探索阶段只有 0 个可进入发布重搜的独立日期方案").

## Key evidence fields

- `final_decision.state = human_block`, `recommended_option_ids = []`, `publication_refreshed_option_ids = []`
- All 3 explorations sealed; 0 publish-live-run (correctly skipped: 0 recommendable)
- `stay_plan_inventory_outcomes`: qunar Maafushi = `confirmed_empty`; qunar Hulhumale = `bounded_provider_pending` / lease-timeout
- qunar-hulhumale-full: terminal FAILED snapshots with `inventory_receipt` (pairs 1–2) / lease-timeout receipt-less (pair 0) — no longer masked
- `planner_verifier_repair_master_stay_plan_chain`: PASS (evidence refs preserved)

## Conclusion

- **passed: false** — reported honestly. Two code-level defects were fixed and verified in a real live E2E (Fix A: masked browser-source failures; Fix B: over-strict ReVerifier gate check).
- The remaining layer-6 failures are **genuine platform-side limitations**: qunar provides no exact Maafushi lodging quotes (frozen 2-provider-per-segment threshold unmeetable) and qunar Hulhumale realtime search does not settle within the bounded wait. Per red lines these are recorded as platform limitations and **not** disguised as passes.
- No real platform write operations were performed (read-only live E2E).
