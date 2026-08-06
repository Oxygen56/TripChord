# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v1.0 Done-Gate 脚本已落地（六层机器门，层 5/6 待用户授权）；本次运行收尾
- **当前分支**：`productization/v1.0`
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：v0.8 secret-redaction 门 + `scripts/run_product_done_gate.py` 六层机器门 + 原子输出 `benchmarks/results/product-v1-done-gate.json`

## 版本状态

| 版本 | 状态 | 退出门摘要 | 证据/说明 |
|---|---|---|---|
| v0.2 动态平台内核 | 有条件通过 | 0/1/2/3/4 平台回放正确 DAG；关闭 scope 零访问；伪造 snapshot 原子拒绝；旧三平台兼容不回退 | `docs/phase-reviews/product-v0.2.md`；deviation：provider selection 已补 DB 迁移（migration `20260806_0002` + `ProviderSelectionRepository`，JSON 降级保留）；真实 Companion 授权本机验证待用户授权 |
| v0.3 全来源终态屏障 | 通过 | Planner 严格晚于最后 Source 终态；无 queued/running 发布路径；零报价无预算 | `docs/phase-reviews/product-v0.3.md` 已更新为「通过」；三项遗留已补 |
| v0.4 跨平台最终方案 | 通过 | A 平台机票 + B 平台酒店；金额/权益无错配；Repair 后 ReVerifier 重算 | 方案 UI 覆盖解释已落地；确定性/税口门保持 |
| v0.5 官方预订跳转 | 核心已落地 | 每个 handoff 可回链；危险 URL 零放行；旧 receipt 不可用 | `platform/handoff.py` + `test_official_handoff.py`（17 项）；待接入 live 重核价 API |
| v0.6 已预订保护 | 核心已落地 | 未解除保护组件修改率 0；解除保护显式确认留痕 | `platform/booking.py` + `test_booking_protection.py`（5 项）；待接入 planning/replan 消费 |
| v0.7 Provider SDK | 核心已落地 | 新 provider 只改 adapter+profile；未认证不进默认选择 | `platform/sdk.py` + `test_provider_sdk.py`（6 项）；未接入 registry/selector 实际使用 |
| v0.8 本地产品体验 | 部分（secret 门已落地） | 全新机器按公开说明完成 replay；秘密不进入日志 | `security/secrets.py` + `test_secret_redaction.py`（redact/contains/policy）；启动器/安装器、向导、WCAG 未做 |
| v0.9 公测可靠性 | 未开始 | Python/Web/Companion/迁移/benchmark/E2E/安全全入 CI | — |
| v1.0 最终产品 | 脚本已落地，门未过 | `run_product_done_gate.py` 六层分门真实通过 | `scripts/run_product_done_gate.py` + `benchmarks/results/product-v1-done-gate.json`；本机层 1/2/3 PASS，层 5/6 `pending user authorization`，`passed=false` |

## 本次运行验证结果（精确）

| 命令 | 结果 |
|---|---|
| `uv run pytest apps/api/tests/` | 786 passed |
| `npm run build` + `npm test` | build 通过；22 Vitest passed |
| `uv run ruff check .` | All checks passed |
| `uv run mypy apps/api/src` | 101 files, no issues |
| `uv run python benchmarks/evaluate.py` 等 4 个 | exit 0 |
| `uv run python scripts/browser_companion_release_gate.py` | PASS（build SHA `6261fdb1…`） |
| `train_sft/train_dpo/policy_reranker --validate-only` | exit 0 |
| `alembic upgrade head` / `alembic check`（临时 DB） | 通过 / No new upgrade operations |
| `npm audit --omit=dev --audit-level=high` | 0 vulnerabilities |
| `git diff --check` | 通过 |
| `scripts/run_product_done_gate.py` | `passed=false`（层 5/6 待用户授权），退出码 2 |

## 当前可对外声明

- v0.3 三项遗留、v0.4 收尾、v0.2 provider-selection DB 迁移、v0.5/v0.6/v0.7 确定性核心、v1.0 Done-Gate 机器门脚本。
- 不做任何 Done-Gate 通过 / 双平台住宿精确报价 / 完整 OTA 闭环声明。

## 绝对不能声明

- 任何"Done-Gate 已通过""双平台住宿精确报价""完整 OTA 闭环"。
- 任何把 login/captcha/pending/empty/timed_out 包装成报价的行为。
- 任何"代码完成=已验证"的表述（v0.5/v0.6/v0.7 仅确定性核心，未接生产路径）。

## 下一条可直接执行的命令

```bash
cd /Users/oxygen/Documents/个人项目/tripchord
uv run python scripts/run_product_done_gate.py
```
（设置 `TRIPCHORD_BROWSER_BRIDGE_TOKEN` 并保持官方 OTA 域名登录后，层 5/6 才会实际执行。）

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
