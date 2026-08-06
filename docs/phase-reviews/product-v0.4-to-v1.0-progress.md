# v0.4 → v1.0 阶段评审：本轮实施延续进度（C-5）

> 结论：**有条件通过（推进中）** —— v0.4 收尾、v0.2 偏差（provider selection DB）、v0.5/v0.6/v0.7 确定性核心、v1.0 六层 Done-Gate 机器可执行脚本已落地并有测试；v0.8/v0.9 与真实平台 canary 仍未完成，按合同第九节如实披露。

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

## 当前仍不能声称

- 任何 v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。
- v0.8/v0.9 的任何交付物；真实平台当前页面可用性（无本机 canary 证据）。
