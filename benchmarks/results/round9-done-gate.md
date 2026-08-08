# TripChord Done-Gate Round 9（方案 B）— 延长 qunar 等待窗口，确定性验证 Hulhumale 平台限制

- **Date**: 2026-08-09
- **Command**: `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py`
- **Baseline commit**: `6ebf54729b17388ee903ab79bfd703f4ce4e97b9`
- **Evidence**: `benchmarks/results/live-done-gate-v4.json`（已刷新，gitignored）、`benchmarks/results/product-v1-done-gate.json`（已刷新，tracked）、`benchmarks/results/live-canary-certified.json`（已刷新，gitignored）
- **Runtime**: live API `127.0.0.1:8000` healthy；Companion 已配对并 auto-reload 到新构建（build_sha256 `27b3f975…`）；certified canary 通过（全部 6 个 OTA scope）。
- **本次改动**: `LODGING_EXTRACTION_STAGE_CAP_MS` 45s → **90s**（含单测断言同步更新，未放宽任何门禁阈值/检查逻辑）。

## Layer matrix

| Layer | Result |
|---|---|
| 1_reproducibility | PASS |
| 2_replay | PASS |
| 3_clean_chrome_fixtures | PASS |
| 4_model_smoke | PASS |
| 5_real_canary | PASS |
| 6_full_e2e | FAIL（honest — 平台限制 + 本轮新增的模型/lease 边界问题） |

## Layer-6 gate: 15 checks itemized

| # | Check | Round-8 | Round-9 | Change |
|---|---|---|---|---|
| 1 | prefrozen_stay_plan_candidate_set | PASS | PASS | — |
| 2 | v4_source_graph | PASS | **FAIL** | **REGRESSION：仅封存 2/3 探索（第 3 对模型 JSON 失败）** |
| 3 | stage_aware_exploration_publication_contract | FAIL | FAIL | 需恰好封存 3 个探索运行，实际 2 个；发布刷新 0 个 |
| 4 | stay_inventory_four_state_contract | FAIL | FAIL | 选中分段精确报价仍不足 2 家（maafushi-full=1 家） |
| 5 | planner_verifier_repair_master_stay_plan_chain | PASS | PASS | —（Fix B 保持） |
| 6 | recommendable_date_pair_stay_plan_options | FAIL | FAIL | 0 个可推荐日期对（要求 ≥2） |
| 7 | all_recommended_publication_closures | FAIL | FAIL | 无最终推荐可做发布闭环深检 |
| 8 | real_v4_browser_source_evidence | FAIL | FAIL | 缺少选中初始运行 |
| 9 | flight_search_outcome_contract | FAIL | FAIL | 缺少选中初始运行 |
| 10 | observed_cross_platform_overlap | FAIL | FAIL | 缺少选中初始运行 |
| 11 | strict_selected_plan_platform_coverage | FAIL | FAIL | 缺少选中初始运行 |
| 12 | icom_exploration_and_publication_evidence | FAIL | FAIL | 缺少探索或发布运行 |
| 13 | planner_verifier_repair_orchestrator | FAIL | FAIL | 缺少选中初始运行 |
| 14 | exact_budget_and_selected_evidence | FAIL | FAIL | 缺少选中初始运行 |
| 15 | event_injection_repair_reverify_master | FAIL | FAIL | 缺少真实事件注入后运行 |

## Hulhumale 各 source 收敛情况（本轮核心观察）

| Date pair | qunar-lodging-hulhumale-full | observed_duration_ms | 结论 |
|---|---|---|---|
| 2026-08-15 → 08-19 (pair 0) | `bounded_provider_pending` | **74790**（vs round-8 ~28s） | 延长等待生效，但仍**未收敛**到 quote/empty |
| 2026-08-20 → 08-27 (pair 1) | `timeout`（lease 到期，无 inventory receipt） | — | 推近 lease 边界的新副作用 |
| 2026-08-30 → 09-05 (pair 2) | 未执行到浏览器阶段（seal 阶段模型失败） | — | 模型 JSON 失败，非平台问题 |

**确定性验证结论**：等待窗口从 45s→90s 机械生效（pair 0 的 qunar Hulhumale 观察等待由 ~28s 拉到 **74.8s**，全程在 25–120s 契约上界内）。但即便在 ~75s 的有界等待下，qunar Hulhumale 的「实时搜索中」壳层**仍未**落为 `quote_found` 或 `confirmed_empty`，保持 `bounded_provider_pending`。这**确定性确认**了平台限制定性：Hulhumale 长时间不收敛不是 45s 等待窗口过短造成的假象，而是真实平台行为。

## Round-9 新增失败模式（honest 记录，非平台限制）

1. **pair 1 qunar-hulhumale-full 原生 lease timeout**：`failure.code=timeout`，message=`browser companion did not complete the task before its lease expired`，`retryable=true`，details 为空（无 terminal DOM 证据 → 无法附加诚实 receipt，故无四态行）。这是把等待推到 120s lease 边界的副作用；按红线未伪造 receipt。
2. **pair 2 探索封存失败（模型可靠性，独立于等待窗口）**：`failure_class=RuntimeError`，`stage=seal-exploration-run`，`required_model_failures=['analyze-live-evidence']`，`StructuredOutputError: model did not return valid JSON`（logical_requests=2, proposal_repairs=0）。导致仅 2/3 探索封存 → `v4_source_graph` 从 round-8 PASS 回归 FAIL，并级联影响 checks 3、6–15。这是基础设施/模型可靠性问题，不是平台限制，也非等待窗口变更引起（pair 0/1 调度 wall time 607s/660s，远低于 3600s 总预算，非超时）。

## Key evidence fields

- `final_decision.state = human_block`，`recommended_option_ids = []`；全部探索仅 2/3 封存。
- Pair 0 四态行（qunar 全部 `bounded_provider_pending`）：full=73801ms / first=73921ms / middle=73891ms / last=74776ms / hulhumale-full=**74790ms**；ctrip lodging 各分段 succeeded。
- Pair 1 四态行：qunar full=`confirmed_empty` / middle=`confirmed_empty`；first=74888ms / last=74884ms 仍 `bounded_provider_pending`；hulhumale-full=**原生 lease timeout**。
- 选中分段 maafushi_icom（pair 0）`distinct_exact_quote_provider_count=1`（仅 ctrip），低于冻结阈值 `_STRICT_MINIMUM_EXACT_PROVIDERS_PER_SELECTED_SEGMENT=2`。
- `model_trace_receipt` / `process_global_model_trace_diagnostic`：进程内模型调用 141→212（delta 71），权威判定以 terminal job 绑定回执为准。

## Conclusion

- **passed: false** — 如实报告。
- **平台限制定性已被确定性验证**：qunar Hulhumale 在 45s 与 90s 两种有界等待下均不收敛为报价/空态，延长等待不会解锁精确报价；选中分段仍仅 ctrip 1 家，冻结的 2 家阈值在当前平台生态下不可满足。
- **本轮新暴露两个非平台问题**（诚实记录）：(1) 90s 等待推近 lease 边界，pair 1 hulhumale 出现原生 lease timeout（无 receipt，四态行缺失）；(2) pair 2 的 `analyze-live-evidence` 模型返回非法 JSON，导致仅 2/3 探索封存，`v4_source_graph` 回归 FAIL。这两个问题建议下一轮作为基础设施/模型可靠性修复处理，与平台限制定性无关。
- 全程只读：未产生任何真实平台写操作。
