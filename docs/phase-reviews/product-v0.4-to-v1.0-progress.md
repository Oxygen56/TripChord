# v0.4 → v1.0 阶段评审：本轮实施延续进度（C-146）

> 结论：**有条件通过（推进中）** —— v0.2–v0.9 的确定性核心与生产接线已落地；v1.0 六层 Done-Gate 的本地工程层可复现。模型费用未确认、真实平台 canary 与全平台 E2E 尚未满足，仍按合同如实保持 `passed=false`。

## v0.4 收尾：方案 UI 覆盖解释 — 通过

| 交付物 | 证据 |
|---|---|
| 逐组件平台/报价时间/到期/覆盖来源/失败终态 | `apps/web/src/domain.ts` `componentCoverageExplanations`；`App.tsx` `LivePackageConsole` 新增 04b 覆盖来源区段；`component-coverage-grid` 渲染 exact_quote / comparison_price_only / bounded_no_exact_quote / failure_terminal |
| Source settlement / exact quote coverage / comparable component 分列统计 | `LivePackageConsole` `coverage-stats` 三列统计 |
| 有证据的省钱/稳妥/少折腾取舍 | `App.tsx` 渲染 `explanation.tradeoffs`（证据不足不凑满三方案） |
| 测试 | `apps/web/src/domain.test.ts` 新增 3 组；22 Vitest 通过；`tsc -b && vite build` 通过 |

## v0.2 偏差：provider selection DB 迁移 — 通过（DB 部分）

- `migrations/versions/20260806_0002_provider_selection.py`；`persistence/provider_selection.py` `ProviderSelectionRepository`（tenant-scoped）。
- `platform/api.py` `_db_selection_store`：DB 优先，JSON 降级（预迁移安装兼容）；`guard_live_start` 改 async。
- 测试：`test_provider_selection_persistence.py`；`test_provider_platform_api.py` 保持通过。
- 真实 Companion 授权 scope 心跳本机验证：**未完成（用户边界）** —— 需用户在本机授权官方域名并保持登录态。

## v0.5 官方预订跳转 — 确定性核心通过

- `platform/handoff.py`：`OfficialDetailLocator`、`HandoffURLPolicy`（逐跳拒绝短链/开放重定向/登录/订单/checkout/payment/coupon/未知 host）、`RevalidationReceipt`（短时、hash-bound、unchanged 才可发 handoff）、`OfficialHandoff`（绑定 plan version/component/offer/query/revalidation receipt；single-use + expired gating）、`ComponentHandoffChecklist`（两步流：先重核价、仅 fresh+unchanged 才去官方页；点击绝不产生 booked）。
- 测试：`test_official_handoff.py` 17 项（含危险 URL 零放行、旧 receipt 不可用）。
- 未接入 live 重核价 API 与前端入口（v0.8 产品体验承接）。

## v0.6 已预订保护 — 确定性核心通过

- `platform/booking.py`：`BookingChecklist/BookingItem`、`UserBookingAcknowledgement`（只有显式用户动作能创建 Booking Fact）、append-only `BookingFact`、`ProtectedComponentConstraint`（贯穿 candidate/optimizer/planner/verifier/repair/reverifier/safety gate/replan）、`ConstraintOverrideRequest`（显式留痕、不自动应用）、`BookingImpact`（Impact Analyzer，阻止受保护组件静默修改）。
- 测试：`test_booking_protection.py` 5 项。
- 未接入 planning/replan 消费路径（v0.6 后续承接）。

## v0.7 Provider SDK — 确定性核心通过

- `platform/sdk.py`：`ProviderAdapter` 公开面、`validate_capability_profile`、`ProviderProfileFixture`（shadow/testing，永不 certified）、`ProviderConformanceRunner`（per provider×vertical 裁决；shadow 不入 Planner/覆盖分母/默认选择）、生命周期状态机 + 一键按垂类 cooldown。
- 测试：`test_provider_sdk.py` 6 项。
- 未接入 registry/selector 的实际使用（现有 v0.2 registry 内核已就绪，SDK 作为外部契约层）。

## v0.8 本地产品体验 — 部分（secret 门已落地）

- `security/secrets.py`：确定性 `redact_secrets` / `contains_secret` / `SecretRedactionPolicy`；main.py 持久化失败日志已脱敏；done-gate 层 1 跑 secret-redaction 测试。
- 测试：`test_secret_redaction.py` 4 项。
- 未做：启动器/安装器、首次设置向导、WCAG 审查（待 v0.8 后续）。

## v1.0 最终产品 Done-Gate — 机器可执行门已落地

- `scripts/run_product_done_gate.py`：六层分门（1 可复现、2 replay、3 干净 Chrome fixture、4 授权模型 smoke、5 真实 canary、6 全平台 E2E），原子输出 `benchmarks/results/product-v1-done-gate.json`。
- 本机运行：层 1/2/3 PASS；层 4 因环境含模型 key 实际运行；层 5/6 如实报告 `pending user authorization`（无 Companion 授权）。
- `passed=false` 且退出码 2，正确反映真实 canary 未过 —— 未伪造通过。

### C-54 返工：层 5 认证 OTA canary + 层 6 真实 E2E 执行器（第五轮）

- **层 5 改为 per-scope 认证 OTA canary**：新增 `benchmarks/live_canary_certified.py`，对默认注册表的 6 个 `certified_active` scope（`ctrip:flight`、`ctrip:lodging`、`qunar:flight`、`qunar:lodging`、`tongcheng:flight`、`icom:transfer`）逐一要求「未过期、已授权、只读」证据：浏览器 scope 需新鲜 Companion 心跳声明 `authorized_scope_keys` 含该 scope；`icom:transfer` 走真实只读公共 API。`run_product_done_gate.py` 层 5 的 PASS 只由该 canary 驱动；open-meteo/故宫 保留为单独标注的「公开页面连通性 canary」（`public_page_connectivity`，`drives_pass=false`），不再驱动层 5。
- **层 6 接入真实 E2E 执行器**：`layer6_full_e2e()` 改为运行 `benchmarks/run_live_done_gate_v4.py`（live job 提交/等待/取消 + 合成 sold_out 事件重规划 + `evaluate_live_v4_done_gate`），删除过时的「gated behind layer 5」占位文案；外部门（bridge token + `TRIPCHORD_ACK_MODEL_COST=1` + 新鲜 Companion）未满足时如实 FAIL（pending user authorization）。
- **本机复跑**（`TRIPCHORD_ACK_MODEL_COST=1`）：层 1/2/3/4 PASS；层 5 FAIL（5 个浏览器 scope 无 Companion 心跳，`icom:transfer` 真实只读 canary PASS）；层 6 FAIL（Companion preflight 失败，未提交实时搜索）；`passed=false` 如实。
- **证据**：`benchmarks/results/product-v1-done-gate.json`、`benchmarks/results/live-canary-certified.json`、`benchmarks/results/live-done-gate-v4.json`（`failed_before_done_gate` / `companion_preflight`）。

### C-146 RETURN 5-P0：正式生产链与回归断点收口（第四十七轮）

- **正式链边界保持**：真实 HTTP 持久任务经独立 worker 使用父 API 持有的 production Companion/Browser、iCom 与实际 model runtime；progress、checkpoint、source 终态、barrier、model trace、cache snapshot 与 signed compact 逐级绑定。C-125 已确认的业务来源、证据绑定、进程/回执一致性与去替身设计本轮不重开、不降级。
- **四项回归闭环**：两个 registry-dispatch failpoint 仅在 Companion ack 200 已完成可观察回写后退出，恢复仍 fail-closed；28.5MB 正式秘密扫描通过线性候选预筛稳定守住原 15 秒预算；gate 测试逐项清除 Git override 并隔离 bridge snapshot；wheel 使用锁文件固定的 Hatchling 后端离线真实构建，随后从隔离目录真实 import 并访问 health。
- **候选验证**：代码/SBOM 候选 `2043384` 上，合并 1790/1790、独立 scripts 626/626、clean-env 626/626、interrupt-window 2/2、扫描与污染/并发组合 5/5、全仓 Ruff、SBOM drift、diff-check 与零残留均通过。三份评审文档同步提交会移动最终 HEAD，故这些候选结果不替代提交后的最终 HEAD 复跑。
- **机器门边界**：文档同步前 run `23fbef3a3a27` 为 L1–3 PASS、L4 SKIP、L5/L6 FAIL，`passed=false`、`evidence_commit=null`、`gate_ref=null`。最终提交后必须重新绑定 API 三哈希并从头运行；未获模型费用确认且未启动 Companion，不声明 Done-Gate 通过。

## 当前仍不能声称

- 任何 v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。
- v0.8/v0.9 的任何交付物；真实平台当前页面可用性（无本机 canary 证据）。
