## TripChord 实施延续（第三轮）交付报告

**一句话结论**：完成 v0.5/v0.6/v0.7 接入生产路径（reprice/handoff 端点 + 预订保护 gate 消费 + 前端两步 handoff 流 + SDK 冷却/一致性 API）、五类反表面端到端验收全 PASS、v0.8 启动器/首次设置向导、v0.9 CI（Companion release gate + 安全扫描 + 基准）与本地可观测性；**v1.0 Done-Gate 未通过**——本机层 1/2/3 PASS，层 4 因未授权模型成本 SKIP，层 5/6 真实平台 canary/E2E 需用户授权官方域名并保持登录态，按合同第九节如实 pending，不伪造。

### 各版本状态与证据

| 版本 | 状态 | 证据 |
|---|---|---|
| v0.2 动态平台内核 | 有条件通过（不变） | `docs/phase-reviews/product-v0.2.md`；provider selection 已 DB 化；真实 Companion 授权待用户 |
| v0.3 全来源终态屏障 | 通过（不变） | `docs/phase-reviews/product-v0.3.md` |
| v0.4 跨平台最终方案 | 通过（不变） | 方案 UI 覆盖解释已落地 |
| v0.5 官方预订跳转 | **生产路径已接入** | `platform/reprice.py`（ComponentRepriceService 严格同 provider×component）+ `persistence/handoff_store.py`（单次使用、不产生 booked）+ `wiring_api.py` reprice/consume 端点 + 前端 `HandoffActionBar` 两步流；`test_reprice_service.py` 9 项 |
| v0.6 已预订保护 | **生产路径已接入** | `platform/booking_gate.py`（BookingProtectionGate/BookingService）+ `persistence/booking_ledger.py`（append-only）+ acknowledge/override/resolve 端点；PlanVerifier 与 ReVerifier 消费同一 ledger（`PROTECTED_COMPONENTS_PRESERVED` 不变量）；`test_booking_gate.py` 11 项 + `test_booking_planning_integration.py` 5 项 |
| v0.7 Provider SDK | **接线完成** | SDK 一致性 runner 校验 registry；`POST /providers/{scope}/cooldown` 一键冷却 + `GET /providers/sdk/conformance`；certified-active 公共 API scope 不再强制 host_permissions |
| v0.8 本地产品体验 | 部分（secret + 启动器/向导已落地） | `scripts/tripchord_launcher.py`（check/setup/wizard/api/web）；`docs/wcag-audit.md`（键盘/焦点/非颜色已补；字号/aria-live 为已知缺口） |
| v0.9 公测可靠性 | 部分（CI/安全/可观测性已接入） | CI 新增 `companion` release-gate、`security`（gitleaks+pip-audit）、acceptance/faults benchmark；`GET /api/v1/observability/summary` |
| v1.0 最终产品 | 门未过（如实） | 层 1/2/3 PASS、层 4 SKIP（`TRIPCHORD_ACK_MODEL_COST` 未授权）、层 5/6 `pending user authorization`；`passed=false` |

### 用户实际能完成的完整流程（现状）

- 本地栈：`uv run python scripts/tripchord_launcher.py setup`（依赖+迁移+Web 构建）→ `api`（loopback API + Bridge）→ `web`；`wizard` 做首次设置检查。
- 搜索与方案、SSE 进度/终态、屏障后一次性结果、逐组件覆盖解释可用。
- **新增**：每个组件可「重核价并查看差异」→ 仅当 receipt 新鲜且 unchanged 时「去官方页面」；官方链接单次有效、绝不自动标记 booked；已预订保护在重规划中被 Verifier/ReVerifier 拦截（需显式解除保护）。
- 真实 OTA 重核价 / canary 需用户授权官方域名并保持登录态（见下）。

### Agent 决策 vs 确定性代码裁决

- 确定性代码新增：reprice 分类与 handoff 逐跳策略、single-use 消费、已预订组件不可静默修改（贯穿 Verifier/ReVerifier）、SDK 一致性/冷却状态机、secret 脱敏与启动器安全。
- Agent 职责不变：需求语义、候选策展、风险批判、Repair 策略、解释。

### 分层验证结果（精确）

- **Python**：`uv run pytest apps/api/tests/ scripts/tests/` 840 passed。
- **Web**：`tsc -b && vite build` 通过；24 Vitest passed（含 2 项 handoff API）。
- **Companion**：release gate PASS（build SHA `6261fdb1…`）。
- **迁移**：`alembic upgrade head` / `alembic check`（临时 DB）通过。
- **benchmark**：`evaluate/evaluate_planning/evaluate_repair/evaluate_events` exit 0；**`evaluate_acceptance.py` 五类反表面全 PASS**（动态矩阵、Planner 晚于全终态、关闭 scope 零访问、危险 handoff URL 零放行、booked 修改率 0）。
- **静态/安全**：`ruff check .` 全绿；`mypy apps/api/src` 107 文件无问题；`npm audit` 0 漏洞；CI 新增 gitleaks secret scan。
- **训练**：`train_sft/train_dpo/policy_reranker --validate-only` exit 0。
- **Done-Gate**：层 1/2/3 PASS；层 4 SKIP（未授权模型成本，不发起付费模型调用）；层 5/6 `pending user authorization`；`passed=false` 退出码 2（如实）。

### replay / fixture / 模型 / 真实 canary 分层结论

- replay/fixture：动态 DAG、ALL_TERMINAL 屏障、handoff URL 策略、booking 约束、SDK 状态机、五类反表面均以本地测试/基准证明。
- 模型：默认 `MODEL_PROVIDER=none`；层 4 需 `TRIPCHORD_ACK_MODEL_COST=1` 且可解析端点才实际运行，未授权不发起付费调用。
- 真实 canary：**未执行**。层 5/6 如实 pending；未访问真实 OTA，同程海外酒店维持用户跳过决定。

### 当前支持与默认选择的 provider × vertical

不变：`ctrip:flight/lodging`、`qunar:flight/lodging`、`tongcheng:flight`、`icom:transfer` 默认选择；`tongcheng:lodging`（用户跳过）、`fliggy` legacy DISABLED。

### 当前仍不能声称的能力

- 任何 v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。
- 真实 OTA 重核价 / 真实 canary / 全平台 E2E（无用户授权）。
- v0.8 完整产品体验（首页工作流拆分、Agent DAG 折叠未做）与 v0.9 全项（浏览器 E2E、SBOM/构建 provenance、Actions SHA 固定、monitor 持久化未完成）。

### Git 状态与本地提交

分支 `productization/v1.0`（**未 push**），工作树干净。本轮 5 个本地提交：`7fc03f5`(v0.5/v0.6/v0.7 接线) → `ada2aee`(前端 handoff 流) → `78cda9a`(v0.8/v0.9) → `7ac3782`(账本/评审/WCAG) → `c17c1e7`(done-gate 证据)。未执行：push、PR、Release、真实平台访问、付费模型调用。

### 外部阻塞（合同第九节，一次一问）

要推进 v1.0 Done-Gate 层 5/6（真实平台 canary 与全平台 E2E），唯一的外部阻塞是：
- **精确阻塞门**：本机需授予官方 OTA 域名（ctrip.com / qunar.com / ly.com / elong.com）的可选 host permission，配好 Chrome Companion 并保持登录态。
- **最小用户动作**：在浏览器中为 Companion 逐项授权上述域名并登录；随后在 `start_live_api.py` 启动的本地 API 环境设置 `TRIPCHORD_BROWSER_BRIDGE_TOKEN`。
- **恢复命令**：`cd /Users/oxygen/Documents/个人项目/tripchord && uv run python scripts/run_product_done_gate.py`。
- **预期成功证据**：层 5/6 从 `pending user authorization` 变为真实 canary/E2E 通过；否则 `passed=false` 保持如实。

（若还需运行层 4 模型 smoke，另需设置 `TRIPCHORD_ACK_MODEL_COST=1` 并提供可解析模型端点——这属于第二问，本轮不阻塞其余工程。）

[@总管](mention://agent/5d294719-0ca8-495b-b3cc-2ea31bebba56)
