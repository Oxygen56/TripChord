# v0.3 阶段评审：全来源终态屏障与诚实发布

> 结论：**有条件通过（核心机制已交付）** —— 调度器原生 `ALL_TERMINAL` 依赖、统一终态模型、SearchRun/SourceAttempt/TerminalReceipt/CompletionBarrier、独立 settle 节点入 live DAG 均已完成并通过全量回归；SSE 屏障前只发进度/终态的前端 gating 未完成，属明确未完成项而非掩盖。

## 1. 原计划交付物逐项核对

| 交付物 | 状态 | 证据 |
|---|---|---|
| 统一跨垂类 Source 终态 | 已验证 | `platform/terminal.py` `SourceTerminalState`（9 态）；`quote_found` 是唯一 `has_planner_quote` |
| SearchRun / SourceAttempt / TerminalReceipt / CompletionBarrier 持久化模型 | 已验证 | `terminal.py`；`test_completion_barrier_releases_only_when_all_terminal`、`test_completion_barrier_holds_on_running_attempt` |
| 调度器原生 ALL_TERMINAL 依赖 | 已验证 | `models.py` `DependencyPolicy`；`runtime.py` `_dependency_met` 应用到 runnable 与 blocked 两处；`test_all_terminal_barrier_releases_on_typed_failure` |
| 独立 settle node；Normalizer 依赖 settle | 已验证 | `live_system.py` 初始 DAG 新增 `settle-source-barrier`（ALL_TERMINAL over all source IDs），`normalize-browser-quotes` 依赖 settle；`_settle_executor` 记录 `barrier_released_at` |
| 禁止靠 success=true 伪装失败来源 | 已验证 | `AgentTaskResult.terminal` 区分 typed terminal 与 `dependency_blocked`；失败 source 保持 `success=False`（`test_all_terminal_barrier_releases_on_typed_failure` 断言 `outcome.succeeded is False`） |
| 到期主动物化 timed_out | 已验证 | `materialize_timed_out_attempts`；`test_timed_out_attempts_are_materialised_at_deadline` |
| 无 queued/running 发布路径 | 已验证 | `CompletionBarrier.released` 仅当全部 attempt TERMINAL；`test_completion_barrier_holds_on_running_attempt` |
| 单来源如实披露；零报价无预算 | 部分 | 底层屏障已支持；live 层由现有 Publication Gate 保证（既有证据），未新增 UI 断言 |
| SSE 屏障前只发进度/终态 | **未完成** | 前端 job SSE 仍按 revision 推送；未在屏障前剥离候选/方案字段 |

## 2. 与产品决定的偏差

1. **SSE 屏障前 gating 未做（明确未完成项）**：路线图要求"屏障前 SSE 只发进度/终态，屏障后一次性给最终结果"。当前 job SSE 事件流已区分阶段，但前端在屏障前是否可能看到部分候选字段未做硬性剥离。列入 v0.3 遗留，不掩盖。
2. **SearchRun 持久化**：模型已建，但未接 SQLite/JSON 落库（沿用进程内 _RunState + live-run-cache）。与 v0.9 可恢复存储一并落地。
3. **ScopeCancellationTombstone 与 generation**：`SourceAttempt.generation` 字段已建，但取消后重试/refresh/failover/event replan 必须复核 tombstone 的完整接线未做。v0.3 后续补。

## 3. 真实运行暴露的假设

- 无真实 OTA 运行在本阶段执行；屏障语义以离线 DAG/调度器回放证明。

## 4. 指标观察

- Python 回归 875 项通过（v0.2 基线 862 + 新增 13 项屏障/终态测试）。
- Ruff / Mypy 全绿。

## 5. 下一版本范围是否成立

v0.4（跨平台最终方案与覆盖解释）范围仍成立，且现有 Planner 已具备"机票/酒店/接驳分别选来源 + package provenance"基础（architecture.md 记载候选可组合不同来源）。v0.4 重点是金额/权益比较身份属性测试与方案 UI 覆盖解释，与 v0.3 无冲突。

## 结论

**有条件通过**。条件：补齐 SSE 屏障前 gating、ScopeCancellationTombstone 完整接线、SearchRun 落库。
