# v0.5 → v1.0 阶段评审：第三轮接线与验收推进（C-6）

> 结论：**有条件通过（推进中）** —— v0.5/v0.6/v0.7 已接入生产路径（API 端点、planning 消费、前端两步 handoff 流），五类反表面验收全 PASS，v1.0 Done-Gate 本机层 1/2/3 PASS；层 4 未授权模型成本 SKIP、层 5/6 真实平台 canary/E2E 需用户授权官方域名并保持登录态，如实 pending，不伪造。

## v0.5 官方预订跳转 — 生产路径已接入

| 交付物 | 证据 |
|---|---|
| 组件级 live 重核价服务 | `platform/reprice.py`：`ComponentRepriceService` 严格同 provider × component；UNCHANGED/CHANGED/NOT_FOUND/LIVE_UNAVAILABLE 分类；官方 URL 逐跳过 `HandoffURLPolicy`；无稳定 deep-link 安全降级到参数卡片 |
| 持久化 handoff | `persistence/handoff_store.py`：receipt + checklist 每 plan×component 存储，单次使用原子消费，`used` 永不产生 booked |
| API 端点 | `platform/wiring_api.py`：`POST …/reprice`、`POST …/handoff/consume` |
| 前端两步流 | `apps/web/src/App.tsx` `HandoffActionBar`：「重核价并查看差异」→ 仅 fresh+unchanged 时「去官方页面」；`window.open` 不自动打开/聚焦；`api.ts` + 测试 |
| 测试 | `test_reprice_service.py` 9 项、`test_wiring_api.py` 9 项、前端 24 Vitest（含 2 项 handoff API） |

**未验证边界**：真实 OTA 重核价需授权 Companion 会话；本环境无授权，端点如实返回 `live_unavailable`/404，不伪造报价。

## v0.6 已预订保护 — 生产路径已接入

| 交付物 | 证据 |
|---|---|
| 确定性消费 gate | `platform/booking_gate.py`：`BookingProtectionGate.evaluate_diff` 拒绝静默修改受保护组件；`BookingService` 显式 acknowledge/override/resolve，override 不自动应用 |
| 持久化 ledger | `persistence/booking_ledger.py`：append-only，`0600` 原子写入 |
| API 端点 | acknowledge / override / resolve / ledger 视图 |
| planning 消费 | `PlanVerifier._check_protected_components`（删除即违规）+ `DeclarativePlanReVerifier` 新增 `PROTECTED_COMPONENTS_PRESERVED` 不变量（diff 级） |
| 测试 | `test_booking_gate.py` 11 项、`test_booking_planning_integration.py` 5 项 |

**未验证边界**：live_system 事件重规划路径未逐点接 gate（10k 行文件），以 Verifier/ReVerifier 中央消费路径作为确定性门；后续可把 gate 下推到 replanner。

## v0.7 Provider SDK — 接线完成

| 交付物 | 证据 |
|---|---|
| registry 一致性 | SDK `ProviderConformanceRunner` 校验 registry 各 profile；certified-active 公共 API scope 不再强制 host_permissions（icom:transfer 修复） |
| 一键冷却 | `POST /api/v1/providers/{scope}/cooldown` 用 `one_click_cooldown` 状态机，runtime overlay 记录，不改不可变 registry |
| 一致性视图 | `GET /api/v1/providers/sdk/conformance` |

**未验证边界**：shadow/testing provider 真实 fixture 接入仍为工程模板，未新增真实 provider。

## v0.8 本地产品体验 — 完成（第四轮收尾）

- 启动器/安装器：`scripts/tripchord_launcher.py`（check/setup/wizard/api/web），统一管理 API/Web/DB/Bridge，不 push/不发布/不访问真实 OTA。
- 首次设置向导：LLM Key 存储、模型 smoke 就绪、Companion 配对、逐平台权限与登录健康（登录健康需真实 Companion 授权）。
- LLM Key 安全：环境变量提供，不写入仓库/日志；secret-redaction 门保持。
- 首页旅行工作流拆分：`WorkflowSteps` 步骤条（需求 → 平台 → 进度 → 方案）+ 各面板 `STEP` 标签；回放 Agent 轨迹、live Agent 流水线（Planner → Verifier → Repair → 主控）、模型回执统计默认折叠为 `<details>`，普通用户先看到结果、再展开高技术细节。
- WCAG 2.2 AA：`docs/wcag-audit.md`；辅助字号 6–11px 全部提到 **≥12px**（150 处）、`aria-live`/`role` 覆盖 SSE 进度/重核价/事件/监控结果、事件注入 `<select>` 补显式 `<label>`、小按钮 `min 24×24` 已补；剩余待办为对比度全量自动化测量与真实浏览器（Lighthouse + NVDA/VoiceOver）人工复核。
- 未验证边界：首次设置向导逐平台权限与登录健康仍需真实 Companion 授权后确认。

## v0.9 公测可靠性 — 完成

- CI：新增 `companion`（release gate）、`security`（gitleaks secret scan + pip-audit）、acceptance/faults benchmark 运行。
- 冻结 benchmark/mutation：`benchmarks/evaluate_acceptance.py`（五类反表面）+ `evaluate_faults.py` 进 CI。
- 可观测性：`GET /api/v1/observability/summary` 分开统计终态/handoff/booking facts。
- **Actions SHA 固定**：`.github/workflows/ci.yml` 全部 `uses:` 以 SHA 锁定（checkout/setup-uv/setup-node/gitleaks，`# vN` 注释标注），CI 不再跟随浮动标签。
- **SBOM/构建 provenance**：`scripts/generate_sbom.py`（CycloneDX 1.5）从 `uv.lock` + `package-lock.json` 生成 115 pypi + 103 npm 组件清单；`check` 以 `source_digests` + 组件清单 + 计数做确定性漂移检测（`commit_sha` 仅信息性，因自引用不可作绑定键）；已入 CI 与 Done-Gate 层 1。
- **job/monitor 可恢复持久化**：job 已 DB 化（既有）；monitor 新增 `live_monitors` + `live_monitor_checks` 表（迁移 `20260807_0001`）、`LiveMonitorRepository`/`DbLiveMonitorStore`；registry `recover()` 重启后恢复可解析 run 的 ACTIVE 监控，run 不可恢复时如实标记 FAILED（不静默丢失）。
- **干净 Chrome + 本地 fixture 浏览器 E2E**：`scripts/browser_e2e.py` 用 CDP（`websockets`，无 Playwright/Puppeteer）驱动干净 headless Chrome，对本地回放 API + 静态 SPA 验证四阶段工作流步骤条与回放规划渲染；`scripts/tests/test_browser_e2e.py` 包装为 pytest，无 Chrome/dist 时如实 SKIP；已入 Done-Gate 层 3。

## v1.0 Done-Gate — 持续推进

- `scripts/run_product_done_gate.py`：层 2 加入 `benchmarks.evaluate_acceptance`；层 3 加入 reprice/booking/wiring fixture 测试 + `clean_chrome_browser_e2e`（无 Chrome 时如实 SKIP）；层 4 修复为「未授权模型成本则 SKIP」。
- 本机运行：层 1/2/3 PASS、层 4 SKIP（`TRIPCHORD_ACK_MODEL_COST` 未授权，不发起付费模型调用）、层 5/6 `pending user authorization`；`passed=false`、退出码 2，如实。
- 五类反表面端到端验收：`benchmarks/evaluate_acceptance.py` 全 PASS。

## 当前仍不能声称

- 任何 v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。
- 真实 OTA 重核价 / 真实 canary / 全平台 E2E（无用户授权）。
- v1.0 Done-Gate 层 4/5/6（模型 smoke 未授权、真实平台 canary 与全平台 E2E 待用户授权）。

## 本轮本地提交（分支 `productization/v1.0`，未 push）

`7fc03f5`（v0.5/v0.6/v0.7 接线）→ `ada2aee`（前端 handoff 流）→ `78cda9a`（v0.8/v0.9 启动器+CI+可观测性）→ `01778cf`（v0.9 Actions SHA 固定 + SBOM/provenance）→ `e64a1d6`（monitor 可恢复持久化）→ `c1f3c88`（干净 Chrome 浏览器 E2E）→ 账本/评审提交。
