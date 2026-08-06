# v0.3 阶段评审：全来源终态屏障与诚实发布

> 结论：**通过** —— C-4 验收列出的三项遗留（SSE 屏障前 gating、ScopeCancellationTombstone 接线、SearchRun 落库）均已补齐并通过测试；调度器原生 `ALL_TERMINAL` 依赖、统一终态模型、SearchRun/SourceAttempt/TerminalReceipt/CompletionBarrier、独立 settle 节点入 live DAG 保持有效。

## 1. 原计划交付物逐项核对

| 交付物 | 状态 | 证据 |
|---|---|---|
| 统一跨垂类 Source 终态 | 已验证 | `platform/terminal.py` `SourceTerminalState`（9 态）；`quote_found` 是唯一 `has_planner_quote` |
| SearchRun / SourceAttempt / TerminalReceipt / CompletionBarrier 持久化模型 | 已验证 | `terminal.py`；`test_completion_barrier_releases_only_when_all_terminal`、`test_completion_barrier_holds_on_running_attempt` |
| SearchRun 落库 | 已验证 | `migrations/versions/20260806_0001`；`persistence/search_runs.py` `SearchRunRepository`；`platform/search_run_builder.py` 把 live run 归约为 typed SearchRun；`test_search_run_persistence.py` |
| 调度器原生 ALL_TERMINAL 依赖 | 已验证 | `models.py` `DependencyPolicy`；`runtime.py` `_dependency_met`；`test_all_terminal_barrier_releases_on_typed_failure` |
| 独立 settle node；Normalizer 依赖 settle | 已验证 | `live_system.py` `settle-source-barrier`（ALL_TERMINAL over all source IDs），`normalize-browser-quotes` 依赖 settle |
| 禁止靠 success=true 伪装失败来源 | 已验证 | `AgentTaskResult.terminal`；失败 source 保持 `success=False` |
| 到期主动物化 timed_out | 已验证 | `materialize_timed_out_attempts`；`test_timed_out_attempts_are_materialised_at_deadline` |
| 无 queued/running 发布路径 | 已验证 | `CompletionBarrier.released`；`test_completion_barrier_holds_on_running_attempt` |
| 单来源如实披露；零报价无预算 | 部分 | live 层由 Publication Gate 保证（既有证据） |
| SSE 屏障前只发进度/终态 | 已验证 | `live_jobs.py` `LiveSourceTerminalEvent` + `barrier_released_at`；job snapshot 仅携带进度/终态；SSE 端点屏障后一次性发 `event: result`；`test_sse_stream_gates_result_until_after_barrier_release`、`test_source_terminal_events_and_barrier_release_survive_success` |
| ScopeCancellationTombstone 完整接线 | 已验证 | `terminal.py` `ScopeCancellationTombstone(Registry)`；`live_system.py` `_source_task_scope` + source executor gate（取消 scope 迟到 attempt 零外部访问、不入 Planner）+ settle 记录 cancelled 源 tombstone；`test_source_executor_suppresses_tombstoned_scope_without_external_call`、`test_scope_cancellation_tombstone_rejects_late_attempts` |

## 2. 与产品决定的偏差

无未完成遗留项。C-4 的三项 v0.3 条件已落地并有对应测试。

## 3. 真实运行暴露的假设

- 无真实 OTA 运行在本阶段执行；屏障语义以离线 DAG/调度器回放证明。

## 4. 指标观察

- Python 回归 776 项通过（本运行全量 `apps/api/tests/`）。
- Ruff / Mypy 全绿；web build + 22 Vitest 通过。

## 5. 下一版本范围是否成立

v0.4（跨平台最终方案与覆盖解释）范围成立；方案 UI 覆盖解释已在 v0.4 评审中落地。

## 结论

**通过**。三项遗留条件均已补齐。
