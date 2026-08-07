# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v0.9 公测可靠性收尾完成；v1.0 Done-Gate 持续推进（第四轮）；六层机器门层 1/2/3 PASS、层 4 因未授权模型成本 SKIP、层 5/6 待用户授权
- **当前分支**：`productization/v1.0`（未 push）
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：v0.9 收尾全项——固定第三方 Actions SHA（CI 不再用 `@v5/@v6` 浮动标签）、SBOM/构建 provenance（`scripts/generate_sbom.py` CycloneDX + 漂移门）、job/monitor 可恢复持久化（monitor 迁入 `live_monitors`/`live_monitor_checks`，重启可恢复）、干净 Chrome + 本地 fixture 浏览器 E2E（`scripts/browser_e2e.py` CDP 驱动，四阶段工作流步骤条 + 回放规划渲染）；此前已完成 v0.8 收尾（首页工作流拆分 + 高技术细节折叠 + WCAG 缺口）、v0.6 收尾（live_system 事件重规划接入 booking gate）与 reprice 两处接线缺陷修复

## 版本状态

| 版本 | 状态 | 退出门摘要 | 证据/说明 |
|---|---|---|---|
| v0.2 动态平台内核 | 有条件通过 | 0/1/2/3/4 平台回放正确 DAG；关闭 scope 零访问；伪造 snapshot 原子拒绝；旧三平台兼容不回退 | `docs/phase-reviews/product-v0.2.md`；deviation：provider selection 已补 DB 迁移（migration `20260806_0002` + `ProviderSelectionRepository`，JSON 降级保留）；真实 Companion 授权本机验证待用户授权 |
| v0.3 全来源终态屏障 | 通过 | Planner 严格晚于最后 Source 终态；无 queued/running 发布路径；零报价无预算 | `docs/phase-reviews/product-v0.3.md` 已更新为「通过」；三项遗留已补 |
| v0.4 跨平台最终方案 | 通过 | A 平台机票 + B 平台酒店；金额/权益无错配；Repair 后 ReVerifier 重算 | 方案 UI 覆盖解释已落地；确定性/税口门保持 |
| v0.5 官方预订跳转 | 生产路径已接入 | 每个 handoff 可回链；危险 URL 零放行；旧 receipt 不可用；无稳定 deep-link 安全降级 | `platform/reprice.py`（ComponentRepriceService 接 live 重核价）+ `persistence/handoff_store.py` + `platform/wiring_api.py` reprice/consume 端点 + 前端 `HandoffActionBar` 两步流；`test_reprice_service.py`（9 项）+ `test_wiring_api.py`；真实 OTA 重核价仍需授权 Companion 会话 |
| v0.6 已预订保护 | **完成** | 未解除保护组件修改率 0；解除保护显式确认留痕；事件重规划不得绕过已预订保护 | `platform/booking_gate.py`（BookingProtectionGate/BookingService）+ `persistence/booking_ledger.py` + acknowledge/override/resolve/ledger 端点；PlanVerifier `_check_protected_components` + ReVerifier `PROTECTED_COMPONENTS_PRESERVED` 不变量；**新增** live_system 事件重规划接入 gate：`DeclarativePackageReVerifier` 增补 `PROTECTED_COMPONENTS_PRESERVED` 检查、`replan_after_event` 逐点接入 `BookingProtectionGate.evaluate_diff`、main.py 事件重规划/周期 monitor 均加载 `run_id` 对应账本，被保护组件被移除/改变且无已应用 override 时进入 HUMAN_BLOCK；`test_booking_gate.py`（11 项）+ `test_booking_planning_integration.py`（5 项）+ `test_package_reverification.py` 新增 4 项 + `test_live_agent_system.py` 新增 2 项 |
| v0.7 Provider SDK | 接线完成 | 新 provider 只改 adapter+profile；未认证不进默认选择 | SDK 一致性 runner 接 registry；`POST /providers/{scope}/cooldown` 一键冷却 + `GET /providers/sdk/conformance`；certified-active 公共 API scope 不再强制 host_permissions |
| v0.8 本地产品体验 | **完成** | 全新机器按公开说明完成 replay；秘密不进入日志；首页旅行工作流拆分；高技术细节默认折叠；WCAG 已知缺口（字号/aria-live）已整改 | `security/secrets.py` + `scripts/tripchord_launcher.py`（check/setup/wizard/api/web）；**本轮新增**：首页 `WorkflowSteps`（需求→平台→进度→方案）+ 各面板 `STEP` 标签；回放 Agent 轨迹、live Agent 流水线、模型回执默认折叠为 `<details>`；`styles.css` 全部辅助字号 6–11px → ≥12px（150 处）、小按钮 `min 24×24`、`aria-live`/`role` 覆盖 SSE 进度/重核价/事件/监控结果、事件注入 `<select>` 补显式 `<label>`；`docs/wcag-audit.md` 已更新（对比度自动化测量与真实浏览器人工复核仍为发布前待办）；首次设置向导逐平台权限与登录健康需真实 Companion 授权后确认 |
| v0.9 公测可靠性 | **完成** | Actions SHA 固定 + SBOM/构建 provenance + job/monitor 可恢复持久化 + 干净 Chrome 浏览器 E2E 全入 CI/Done-Gate | `ci.yml` 全部 `uses:` 以 SHA 锁定；`scripts/generate_sbom.py` CycloneDX（115 pypi + 103 npm）+ 确定性漂移门入层 1；monitor 迁移 `20260807_0001`（`live_monitors`/`live_monitor_checks`）+ `recover()` 重启恢复，run 不可恢复如实 FAILED；`scripts/browser_e2e.py` CDP 驱动干净 headless Chrome 验证四阶段工作流步骤条 + 回放规划渲染（证据 `benchmarks/results/browser-e2e.json` + 截图），入层 3 |
| v1.0 最终产品 | 脚本持续推进，门未过 | `run_product_done_gate.py` 六层分门真实通过 | 本机层 1/2/3 PASS（层 3 含 `clean_chrome_browser_e2e`）；层 4 SKIP（`TRIPCHORD_ACK_MODEL_COST` 未授权，不发起付费模型调用）；层 5/6 `pending user authorization`；`passed=false` 如实；`benchmarks/evaluate_acceptance.py` 五类反表面全 PASS |

## 本次运行验证结果（精确）

| 命令 | 结果 |
|---|---|
| `uv run pytest apps/api/tests/ scripts/tests/` | 855 passed（新增 `test_browser_e2e.py` + `test_live_monitor_persistence.py` 4 项等） |
| `npm run build` + `npm test` | build 通过；24 Vitest passed |
| `uv run ruff check .` | All checks passed |
| `uv run mypy apps/api/src` | 108 files, no issues |
| `uv run python benchmarks/evaluate.py` 等 4 个 | exit 0 |
| `uv run python benchmarks/evaluate_acceptance.py` | 五类反表面全 PASS（原子输出 `benchmarks/results/product-acceptance.json`） |
| `uv run python scripts/tripchord_launcher.py check/wizard` | 通过；不访问真实 OTA、不发起付费模型调用 |
| `uv run python scripts/browser_companion_release_gate.py` | PASS（build SHA `6261fdb1…`） |
| `uv run python scripts/browser_e2e.py` | 11/11 断言 PASS（工作流步骤条 + 回放规划渲染；证据 `benchmarks/results/browser-e2e.json` + 截图） |
| `uv run python scripts/generate_sbom.py check` | 通过（`source_digests` 绑定，115 pypi + 103 npm 无漂移） |
| `train_sft/train_dpo/policy_reranker --validate-only` | exit 0 |
| `alembic upgrade head` / `alembic check`（临时 DB） | 通过 / No new upgrade operations |
| `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities |
| `git diff --check` | 通过 |
| `scripts/run_product_done_gate.py` | 层 1/2/3 PASS（层 3 含 `clean_chrome_browser_e2e`）；层 4 SKIP（未授权模型成本）；层 5/6 `pending user authorization`；`passed=false`，退出码 2（如实） |

## 当前可对外声明

- v0.5/v0.6/v0.7 接入生产路径：reprice/handoff 端点 + 前端两步 handoff 流；预订保护 gate 被 Verifier/ReVerifier 与 live_system 事件重规划共同消费（v0.6 收尾完成）；SDK 冷却/一致性 API 接线。
- v0.8 完整本地产品体验：启动器/向导 + 首页旅行工作流拆分 + 高技术细节默认折叠 + WCAG 已知缺口整改（字号 ≥12px / aria-live / 表单标签 / 目标尺寸）；v0.9 CI（Companion release gate + 安全扫描 + acceptance/faults benchmark）、本地可观测性端点。
- v0.9 收尾完成：第三方 Actions SHA 固定（CI 不再跟随 `@v5/@v6` 浮动标签）、CycloneDX SBOM + 构建 provenance 漂移门（`source_digests` 绑定，避免 `commit_sha` 自引用失效）、job/monitor 可恢复持久化（重启后 ACTIVE 监控自动续跑、run 不可恢复如实 FAILED）、干净 Chrome + 本地 fixture 浏览器 E2E（CDP 驱动，无 Playwright/Puppeteer，验证四阶段工作流步骤条与回放规划渲染）。
- 五类反表面端到端验收全 PASS（`benchmarks/evaluate_acceptance.py`）。
- 不做任何 Done-Gate 通过 / 双平台住宿精确报价 / 完整 OTA 闭环声明。

## 绝对不能声明

- 任何"Done-Gate 已通过""双平台住宿精确报价""完整 OTA 闭环"。
- 任何把 login/captcha/pending/empty/timed_out 包装成报价的行为。
- 任何"代码完成=已验证"的表述：真实 OTA 重核价、真实 canary、全平台 E2E 均未执行（需用户授权官方域名并保持登录态）。

## 下一条可直接执行的命令

```bash
cd /Users/oxygen/Documents/个人项目/tripchord
uv run python scripts/run_product_done_gate.py
```
（设置 `TRIPCHORD_BROWSER_BRIDGE_TOKEN` 并保持官方 OTA 域名登录后，层 5/6 才会实际执行；设置 `TRIPCHORD_ACK_MODEL_COST=1` 且提供可解析模型端点后，层 4 才会实际运行。）

## 基线记录（业务代码修改前）

### 环境

- uv 0.11.26、Node v26.4.0、npm 11.17.0、docker 可用；项目解释器 Python 3.12（uv 管理）。
- 依赖 `uv sync --locked --all-groups` 成功。

### 基线命令结果（全部通过，无基线失败）

| 命令 | 结果 |
|---|---|
| `git diff --check` | 通过 |
| `uv run ruff check .` | 通过 |
| `uv run mypy apps/api/src` | 通过（88 文件） |
| `uv run pytest` | 通过（844 passed） |
| `npm ci` | 通过（0 漏洞） |
| `npm run build` | 通过 |
| `npm test` | 通过（19 passed） |
| `npm audit --omit=dev --audit-level=high` | 0 漏洞 |
| `uv run python scripts/browser_companion_release_gate.py` | PASS |
| `uv run python -m training.train_sft --validate-only` | 通过 |
| `uv run python -m training.train_dpo --validate-only` | 通过 |
| `uv run python -m training.policy_reranker` | 通过 |
| `uv run python benchmarks/evaluate.py` | 通过 |
| `uv run python benchmarks/evaluate_planning.py` | 通过 |
| `uv run python benchmarks/evaluate_repair.py` | 通过 |
| `uv run python benchmarks/evaluate_events.py` | 通过 |
| 迁移 `upgrade head` / `alembic check` | 通过 |
| `docker compose config` | 通过 |

### 迁移清单：固定三平台 / 旧假设落点（v0.2 处理）

已扫描并处理：

- [x] `planning/flexible_dates.py`：`FlexibleDateExplorer`/`FlexibleQueryPlanBuilder` 改为"至少一个唯一平台"；`QueryPlanPolicy.validate_platforms` 改为动态平台集；错误文案改为 provider 数无关。
- [x] `agents/live_system.py`：`LivePackageAgentSystem` 改为"至少一个唯一 provider"。
- [x] `agents/flexible_live_system.py`：`_source_delays` 13/18 任务剖面改为动态 provider 数。
- [x] `main.py`：三处 strict three-platform 文案改为 full-coverage across selected scopes；装配改为 registry 派生平台集。
- [x] `agents/live_done_gate.py`：保持 `_EXPECTED_PROVIDERS` 历史契约（v3 旧 gate 不得削弱）。
- [x] 新增 `platform/` 内核：capability / registry / selection / adapters / api。
- [x] 前端 `App.tsx` / `api.ts` 固定 union 与 "3/3" 标签（v0.2 起已由 ProviderMatrix 与动态 capability 响应替代）。
- [x] Companion 逐 scope host permission 与 scope-aware heartbeat（v0.2 内核已实现；真实本机授权验证待用户）。
