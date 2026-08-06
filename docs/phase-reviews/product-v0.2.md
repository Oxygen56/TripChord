# v0.2 阶段评审：动态平台内核

> 结论：**有条件通过** —— 后端动态平台内核、能力矩阵 API、Scope-aware Companion 心跳与前端矩阵 UI 已完成并通过全量回归；用户选择持久化采用本地原子 JSON 存储而非数据库迁移（偏差已记录），真实 Companion 域名授权仍未在本机验证。

## 1. 原计划交付物逐项核对

| 交付物 | 状态 | 证据 |
|---|---|---|
| `ProviderScopeKey(provider, vertical)` 稳定身份 | 已验证 | `apps/api/src/tripchord/platform/capability.py`；`test_scope_key_identity_is_stable_and_hashable` |
| `ProviderCapability` 与认证阶段 | 已验证 | `capability.py`；`CertificationStage` 六态；默认 profile 反映 2026-08-05 审计边界 |
| 用户选择模型 | 已验证 | `selection.py` `UserScopeSelection` / `UserScopeSelectionSet`；`test_user_disabled_scope_never_selected` |
| Eligibility 计算 | 已验证 | `compute_eligible_scope_keys`；认证 ∩ 垂类 ∩ 授权 ∩ 连接 ∩ 非冷却 ∩ 用户开启；`test_eligible_flight_and_lodging_matches_documented_scope` |
| 不可变 Selection Snapshot（含 SHA） | 已验证 | `build_selection_snapshot` + `verify()`；`test_forged_snapshot_hash_is_atomically_rejected`、`test_forged_provider_id_changes_hash_and_fails_verification` |
| 版本化 capability profile 与旧 profile 兼容 | 已验证 | `build_default_registry` / `build_legacy_v4_registry`；Fliggy 保留为 legacy DISABLED；`test_legacy_v4_profile_is_preserved_not_mutated` |
| Registry 替代固定三平台 | 已验证 | `adapters.py` 从 snapshot 派生平台集；`main.py` 装配改用 registry 派生；`flexible_dates` / `live_system` / `flexible_live_system` 放宽 exactly-three 约束 |
| 0/1/2/3/4 平台回放正确 DAG | 已验证 | `test_dynamic_provider_count_builds_correct_task_set`（1/2/3/4 平台）；0 平台 `test_zero_provider_query_plan_refuses_cleanly` |
| 每垂类至少一个合格来源，为 0 拒绝启动 | 已验证 | `guard_live_start` + `requested_verticals_without_eligible_scope`；`test_zero_eligible_vertical_blocks_startup` |
| 关闭 scope 零访问 | 已验证 | 用户关闭 scope → 不 SELECTED；registry DISABLED scope 不产生任务；`test_user_disabled_scope_never_selected` |
| 伪造 provider/snapshot hash 原子拒绝 | 已验证 | `verify()` 重算；两处反例测试 |
| 旧三平台兼容不回退 | 已验证 | 全量 862 项 Python + 19 项 Web + Companion release gate 全部通过 |
| API 能力矩阵 / 健康 / 用户开关 | 已验证 | `GET /api/v1/providers/capabilities`、`GET /api/v1/providers/runtime-health`、`PUT /api/v1/preferences/provider-selection`；`test_provider_platform_api.py` |
| Companion 逐 scope 心跳 | 已验证 | `background.js` heartbeat/claim 报告 `authorized_scope_keys`/`adapter_version`/`contract_version`/`runtime_instance_id`；后端模型与存储记录 |
| 前端能力矩阵 UI | 已验证 | `ProviderMatrix` 组件 + capability-table 样式；`npm run build` / `npm test` 通过 |
| Registry 与数据库迁移 | **偏差** | 未新增数据库迁移；用户选择用本地原子 JSON（`.runtime/provider-selection.json`，0600）。见「偏差」 |

## 2. 与产品决定的偏差

1. **数据库迁移缺失（合理变更）**：路线图要求"新建 provider capability、user selection、eligibility snapshot 和 selection snapshot 模型及迁移"。本次把能力/快照建模为确定性领域契约（不落库，因 Registry 是代码事实源），用户选择用原子 JSON 存储。这与既有 `PersistentMemoryStore`（`.runtime/memory-state.json`）模式一致，且符合本地优先；多租户持久化选择表推迟到 v0.9（可恢复存储/多实例）一并落地。不是功能缺失，但需在 v0.9 前补齐 DB 迁移。
2. **`live_done_gate_v4.py` 内硬编码"三平台"文案未全改**：v0.4 之前的 Done-Gate 仍按冻结场景语义评估；历史 v4 证据包（Round 14-17）必须保持可回放，因此未改其断言语义。这是保守兼容，不是缺陷。
3. **真实 Companion 域名授权未在本机验证**：心跳按"实际授权 scope"上报的代码已完成并通过 JS 合同测试，但未在真实 Chrome 会话中观察授权后的 `authorized_scope_keys`。这是外部验证边界，不阻塞代码门。

## 3. 真实运行暴露的假设

- 无真实 OTA 运行在本阶段执行；能力矩阵与快照均以 2026-08-05 已审计边界为事实源，未新增平台声明。

## 4. 指标观察

- Python 回归 862 项通过（基线 844 + 新增 18 项平台内核/API/DAG 测试）。
- Web 构建 + 19 项 Vitest 通过；Companion JS 四组合同 + release gate PASS（build SHA `6261fdb1…`）。
- Ruff / Mypy 全绿。

## 5. 下一版本范围是否成立

v0.3（全来源终态屏障）范围仍成立。前置条件：Selection Snapshot 已可冻结，`guard_live_start` 已拒绝 0 合格垂类；v0.3 需要把调度器的 `ALL_SUCCEEDED` 语义升级为 `ALL_TERMINAL`，新增 SearchRun/SourceAttempt/TerminalReceipt/CompletionBarrier 持久化模型，并保证 Planner 严格晚于最后 Source 终态。这些与 v0.2 无冲突。

## 结论

**有条件通过**。条件：在 v0.9 前为 provider selection 补数据库迁移；在真实 Companion 会话中验证 scope 心跳。
