# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v0.2 实施中（动态平台内核后端已完成并测试）
- **当前分支**：`productization/v1.0`
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：v0.2 平台内核后端（registry/snapshot/API/guard）+ 全部通过；待办前端能力矩阵与 Companion 心跳

## 版本状态

| 版本 | 状态 | 退出门摘要 | 证据/说明 |
|---|---|---|---|
| v0.2 动态平台内核 | 实现中 | 0/1/2/3/4 平台回放正确 DAG；关闭 scope 零访问；伪造 snapshot 原子拒绝；旧三平台兼容不回退 | 后端核心已完成：`platform/` 模块、API 矩阵、guard；前端/Companion 待完成 |
| v0.3 全来源终态屏障 | 未开始 | Planner 严格晚于最后 Source 终态；无 queued/running 发布路径；零报价无预算 | — |
| v0.4 跨平台最终方案 | 未开始 | A 平台机票 + B 平台酒店；金额/权益无错配；Repair 后 ReVerifier 重算 | — |
| v0.5 官方预订跳转 | 未开始 | 每个 handoff 可回链；危险 URL 零放行；旧 receipt 不可用 | — |
| v0.6 已预订保护 | 未开始 | 未解除保护组件修改率 0；解除保护显式确认留痕 | — |
| v0.7 Provider SDK | 未开始 | 新 provider 只改 adapter+profile；未认证不进默认选择 | — |
| v0.8 本地产品体验 | 未开始 | 全新机器按公开说明完成 replay；秘密不进入日志 | — |
| v0.9 公测可靠性 | 未开始 | Python/Web/Companion/迁移/benchmark/E2E/安全全入 CI | — |
| v1.0 最终产品 | 未开始 | `run_product_done_gate.py` 六层分门真实通过 | — |

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
- [ ] 前端 `App.tsx` / `api.ts` 固定 union 与 "3/3" 标签（待完成）。
- [ ] Companion 逐 scope host permission 与 scope-aware heartbeat（待完成）。

## 当前可对外声明

- 仓库完整独立，默认 `MODEL_PROVIDER=none` 不产生付费调用。
- v0.2 动态平台内核后端已实现：`ProviderScopeKey`/`ProviderCapability`/`ProviderRegistry`/`SelectionSnapshot`；0/1/2/3/4 平台 DAG 可构建；关闭 scope 不产生任务；伪造 snapshot hash 原子拒绝；旧三平台测试不回退。
- 新增 API：`GET /api/v1/providers/capabilities`、`GET /api/v1/providers/runtime-health`、`PUT /api/v1/preferences/provider-selection`。
- 尚未声明：任何 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。

## 绝对不能声明

- 任何"Done-Gate 已通过""双平台住宿精确报价""完整 OTA 闭环"。
- 任何把 login/captcha/pending/empty/timed_out 包装成报价的行为。
- 任何"代码完成=已验证"的表述。

## 下一条可直接执行的命令

```bash
cd /Users/oxygen/Documents/个人项目/tripchord
uv run pytest && uv run ruff check . && uv run mypy apps/api/src
```
