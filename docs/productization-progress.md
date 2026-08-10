# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v1.0 Done-Gate 第十七轮（C-118，监督退回续跑·修复）——按监督复核的八项硬缺口原地修复：① CAS 成功后 post-CAS re-dump 失败不翻转退出码（passed=true 指针已装）；② 层 5 同时要求 canary 退出码 0 + certified JSON passed=true + 精确六 scope 逐项 fresh/authorized/read_only/passed（非零退出+伪全绿 JSON 必败）；③ 所有退出路径在 report/manifest/compact 写完后执行秘密扫描，`_run` 源头脱敏 + `_dump` 落盘脱敏，失败门/不提交运行不写盘不打印子进程 stdout/Cookie/Authorization/API key/账号/完整 tracking URL，错误不回显原文；④ 层 6 residual lease 预检读取实际 `TRIPCHORD_BROWSER_BRIDGE_STATE_PATH` 对应 JSON 的 queued/claimed/重排状态（不只查 tripchord.db）；⑤ evidence commit 全程临时 index，并发 HEAD/CAS 失败绝不 reset 分支或真实 index 到旧 S；⑥ staging 根写入前 lstat 拒 symlink 且独占创建（已有空目录也拒绝），所有输出原子 0600；⑦ compact 合同从证据提交 E 的 blob 回读硬校验（层 5 精确六 scope、层 6 精确十五项全部通过 + 结构化报价绑定/覆盖阈值/P-V-R-ReV/预算/事件注入重规划/repo/runtime/Companion identity）；⑧ 账本与交付声明更正。**C-118 更正**：C-116 打回 C-114 后，本轮回不提前宣称通过；真实门未过、E 未生成前不宣称 compact 已提交。
- **当前分支**：`productization/v1.0`（未 push；API 已在最终代码+文档全部提交后的最终 HEAD 受控重启并硬校验 provenance，机器证据见第十七轮评审 §5 与 issue 评论）
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：C-118 第十七轮——八项硬缺口全部修复并有真实临时仓库/端到端反例测试（gate 测试 138 项通过）；隔离回归 apps/api/tests + gate 单测 1016 项通过；Companion 自动重配结果与六层门重跑如实记录（见第十七轮评审），passed=true 前不声称通过

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
| v1.0 最终产品 | 脚本持续推进，门未过 | `run_product_done_gate.py` 六层分门真实通过 | **第六轮（C-63）**：HEAD=`25973b1` 重跑完整 done-gate（`TRIPCHORD_ACK_MODEL_COST=1`），证据 `commit_sha` 刷新为 `25973b15a…`（与修复提交一致），各层状态不变；层 1/2/3/4 PASS（层 4 模型 smoke 实际运行通过）；层 5 FAIL——per-scope 认证 OTA canary（6 个 certified scope）5 个浏览器 scope 无 Companion 心跳（companion_status=disconnected、0 companions），`icom:transfer` 真实只读公共 API canary PASS（7 个选项）；层 6 FAIL——`run_live_done_gate_v4.py` Companion preflight 失败（stage=companion_preflight）；`passed=false` 退出码 2 如实；`benchmarks/evaluate_acceptance.py` 五类反表面全 PASS。**第十五轮（C-113，监督续跑）**：先修根因再等配对——层 2/3 宿主 bridge-state 泄漏修复（`apps/api/tests/conftest.py` session 级清 env + 取消/高并发/重启零残留 3 个反例）；证据合同强制输入清单（层 5/6 原始 evidence 缺文件 exit 2）+ 提交后字段/哈希/父链硬校验；证据落盘 0700/0600/反符号与硬链接/非当前用户 + 多类秘密扫描（模型 key/bridge token/Cookie/Authorization/账号标识/完整 tracking URL）；两阶段 commit 改为 `commit-tree` + 原子 `update-ref` CAS（任一阶段失败 HEAD 不动、无中间 E、index/worktree 干净、报告 evidence_commit 清空）；API `launchctl kickstart -k` 受控重启绑定 HEAD=`e862a98`（provenance 三哈希匹配，但该 HEAD 是中间态，见下）。77 个 gate 测试通过；层 5/6 仍待用户配对 Companion。**第十六轮（C-114，独立审查续跑）**：审查提出 R1–R8 八项硬性要求，全部修复并有真实临时仓库反例测试——层 6 按真实 `done_gate.checks` 15 项合同校验；层 5 只信 certified canary JSON（passed + 完整 6 scope + 每项 fresh/authorized/read_only/passed）；staging 独占新建且初始为空并绑定唯一 run_id/tested SHA/generated_at；层 3 浏览器 E2E exit 2（skip）不算通过；秘密扫描 OSError fail-closed 且覆盖 OPENAI/ANTHROPIC 等全部模型 key、错误只报种类/文件不回显；compact 证据含 15 项 checks/覆盖阈值/P-V-R/预算/事件重规划/identity 并提交后从 E 读回复核；staging 根与子目录 lstat 拒 symlink/hardlink/非当前用户、`--output` 原子 0600；只读 live-state lease preflight 检测 queued/claimed 残留 lease（R7，快照隔离测试）；文档「唯一只差用户配对 / API 已绑定最终 HEAD」结论更正（R8）。**gate 测试 121 项通过**；最终代码+文档提交后再在真实最终 HEAD 受控重启。**第十七轮（C-118，监督退回续跑）**：八项硬缺口全部修复并有真实临时仓库/端到端反例测试——CAS 后 post-CAS re-dump 失败不翻转退出码；层 5 同时要求 canary 退出码 0 + certified JSON passed=true + 精确六 scope；所有退出路径写后扫密 + `_run`/`_dump` 源头脱敏（失败门/不提交运行不写盘不打印子进程 stdout/Cookie/Authorization/API key/账号/完整 tracking URL）；层 6 lease 预检读取实际 `TRIPCHORD_BROWSER_BRIDGE_STATE_PATH` JSON 的 queued/claimed/重排状态；evidence commit 全程临时 index、并发 HEAD/CAS 失败不 reset 分支或真实 index 到旧 S；staging 根 lstat 拒 symlink + 独占创建（已有空目录也拒绝）+ 输出原子 0600；compact 合同从 E 的 blob 回读硬校验（层 5 精确六 scope、层 6 精确十五项全部通过 + 绑定字段）；账本与交付声明更正。**gate 测试 138 项通过**；隔离回归 apps/api/tests + gate 单测 1016 项通过；最终代码+文档提交后在真实最终 HEAD 受控重启、Companion 自动重配、从头跑六层门如实记录（见第十七轮评审）。 |

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
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py`（第六轮 C-63，HEAD=`25973b1`） | 层 1/2/3/4 PASS（层 4 模型 smoke 实际运行通过）；层 5 FAIL——认证 OTA canary 5/6 scope 待用户授权（`icom:transfer` PASS）；层 6 FAIL——`run_live_done_gate_v4.py` Companion preflight 失败（stage=companion_preflight）；证据 `product-v1-done-gate.json` `commit_sha=25973b15a…` 与 HEAD 一致；`passed=false`，退出码 2（如实） |
| `uv run python benchmarks/live_canary_certified.py --bridge-token <token>` | 退出码 2（如实）；`benchmarks/results/live-canary-certified.json` per-scope 证据（fresh/authorized/read-only）；`icom:transfer` 真实只读公共 API 返回 7 个选项 |

### 第十五轮（C-113，监督续跑）验证结果（本轮）

| 命令 | 结果 |
|---|---|
| `TRIPCHORD_BROWSER_BRIDGE_STATE_PATH=<live-state> uv run pytest scripts/tests/test_run_product_done_gate.py apps/api/tests/test_browser_cancellation.py -q` | 全部通过（含层 2/3 反例 + 取消/高并发/重启零残留 + 证据合同/落盘/原子性反例），在真实 gate 环境（宿主 bridge-state 存在）下确认隔离修复生效 |
| `uv run pytest scripts/tests/ apps/api/tests/test_browser_cancellation.py`（gate 环境） | 全部通过（不跑 `test_browser_e2e.py`，避免污染 tracked 结果树） |
| `scripts/run_product_done_gate.py` 单测 | 77 passed——含层5/6 原始 evidence 缺文件 exit 2、symlink/hardlink/foreign-uid 拒绝、多类秘密泄漏（tracking URL/Authorization/账号 id/模型 key）、phase-1/2 add/commit-tree/update-ref CAS 失败原子性、成功父链 S→E→P |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启成功；`/api/v1/agents/runtime` provenance `commit_sha=e862a98…`、lock/source SHA 与本地树三哈希完全匹配；bridge token 保留（60 位未变）、model key 加载、`browser_companion_supervisor_running=true` |
| Companion 状态 | `GET /browser-bridge/v1/companions/status` → 0 companions（浏览器配对仍为用户侧待办） |

### 第十七轮（C-118，监督退回续跑·修复）验证结果（本轮）

| 命令 | 结果 |
|---|---|
| `.venv/bin/python -m pytest scripts/tests/test_run_product_done_gate.py` | 138 passed——含八项硬缺口反例：CAS 后 post-CAS re-dump 失败 rc=0、层 5 非零退出+伪全绿 JSON 必败、额外 scope 必败、失败门报告脱敏、bridge-state queued/claimed/重排预检、层 6 bridge 残留阻断、staging symlink 拒绝、compact 层5/6 blob 合同接受与拒绝 |
| `uv run pytest apps/api/tests/ scripts/tests/ --ignore scripts/tests/test_browser_e2e.py` | 1016 passed，退出码 0（不跑 `test_browser_e2e.py`，避免污染 tracked 结果树） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启成功；`/api/v1/agents/runtime` provenance `commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 与本地树三哈希匹配、pid 存活（机器证据见第十七轮评审 §5） |
| Companion 自动重配 | supervisor_running=true、outcome=`waiting_for_control_capable_companion`、attempt_count=0；companion status 0 companions/disconnected；bridge-state `tasks=[]`/`reload_requests=[]` 无残留；未配对任何 Companion（用户侧动作，本轮不伪造；详见第十七轮评审 §6） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit-evidence` | run_id=`ff7492050865`、tested_commit_sha=`048ba57`：层 1/2/3/4 PASS、层 5 FAIL（5 浏览器 scope 无 Companion 心跳，`icom:transfer` 真实公共 API 7 选项 PASS）、层 6 FAIL（pending user authorization、runner `failed_before_done_gate`）；`passed=false` 退出码 2、evidence_commit=None、工作树干净；15 项 done_gate checks 未运行；详见第十七轮评审 §7 |

### 第十八轮（C-122，Round-18 六项硬门复核修复）验证结果

| 命令 | 结果 |
|---|---|
| `uv run ruff check .` | All checks passed（0 错误） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py` | 249 passed——含六项硬门反例：层 5 六 scope 逐项 kind/provider/正整数 iCom options/报价 sample/Companion 绑定/心跳回执/严格 64hex build 指纹；层 6 十五项逐项语义（source 图任务数/唯一 id、stage 3+2 合同、source==snapshot、3 间隔 3 provider 重叠、三 OTA 集合、iCom 全覆盖 + 非空发布目标）；resolver 固定六层名/skipped=false/`--latest` 参数冲突/发布回执丢失与对账读失败终态；secret scan 裸 token/cookie/secret/browser_token 字段名与未知 64hex 拒绝、E/P 提交固定非个人身份；naive lease/naive saved_at fail-closed、正负偏移反例 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests`（clean-env 全量回归） | 1128 passed，退出码 0（含 scripts/tests + apps/api/tests） |
| `uv run python scripts/tests/run_tests_clean_env.py benchmarks/tests apps/browser-companion/tests` | 421 passed，退出码 0 |
| `cd apps/web && npm test` | 24 passed，退出码 0 |
| `uv run pytest apps/api/tests/test_icom_transfer.py` | 11 passed——`TRAVEL_DATE` 改为相对今天 +30 天（日期炸弹修复：旧的固定 2026-08-10 在当天到来后 departure 已过、转换被静默置 None） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | 受控重启绑定**本提交（round-18 最终 HEAD）**，provenance 三哈希匹配、pid 存活（机器证据见 gate run 发布的 side-channel evidence 与最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit-evidence` | 从零重跑严格六层门，绑定 round-18 最终 HEAD；`passed=false` 与证据提交如实记录在最终结果评论 |

## 当前可对外声明

- v0.5/v0.6/v0.7 接入生产路径：reprice/handoff 端点 + 前端两步 handoff 流；预订保护 gate 被 Verifier/ReVerifier 与 live_system 事件重规划共同消费（v0.6 收尾完成）；SDK 冷却/一致性 API 接线。
- v0.8 完整本地产品体验：启动器/向导 + 首页旅行工作流拆分 + 高技术细节默认折叠 + WCAG 已知缺口整改（字号 ≥12px / aria-live / 表单标签 / 目标尺寸）；v0.9 CI（Companion release gate + 安全扫描 + acceptance/faults benchmark）、本地可观测性端点。
- v0.9 收尾完成：第三方 Actions SHA 固定（CI 不再跟随 `@v5/@v6` 浮动标签）、CycloneDX SBOM + 构建 provenance 漂移门（`source_digests` 绑定，避免 `commit_sha` 自引用失效）、job/monitor 可恢复持久化（重启后 ACTIVE 监控自动续跑、run 不可恢复如实 FAILED）、干净 Chrome + 本地 fixture 浏览器 E2E（CDP 驱动，无 Playwright/Puppeteer，验证四阶段工作流步骤条与回放规划渲染）。
- 五类反表面端到端验收全 PASS（`benchmarks/evaluate_acceptance.py`）。
- C-54 返工完成：层 5 改为 per-scope 认证 OTA canary（`live_canary_certified.py`，6 个 certified scope 逐项 fresh/authorized/read-only 证据，open-meteo/故宫 仅作公开页面连通性标注）；层 6 接入 `run_live_done_gate_v4.py` 真实 E2E 执行器（删除「gated behind layer 5」误导文案）。本机复跑层 1/2/3/4 PASS、层 5/6 如实 FAIL（pending user authorization）。
- **当前支持与默认选择的 provider × vertical（certified-active 精确集合，6 scope）**：`ctrip:flight`、`ctrip:lodging`、`qunar:flight`、`qunar:lodging`、`tongcheng:flight`、`icom:transfer`（默认选择按 certified-active 过滤，见 `platform/registry.py` `build_default_registry()`）。`tongcheng:lodging`（用户 2026-08-05 跳过）与 `fliggy:flight/lodging`（2026-08-04 验证门失败移出活跃矩阵，仅存于 `LEGACY_V4_CAPABILITIES`）均为 **DISABLED**，不在活跃矩阵。
- 不做任何 Done-Gate 通过 / 双平台住宿精确报价 / 完整 OTA 闭环声明。

## 绝对不能声明

- 任何"Done-Gate 已通过""双平台住宿精确报价""完整 OTA 闭环"。
- 任何把 login/captcha/pending/empty/timed_out 包装成报价的行为。
- 任何"代码完成=已验证"的表述：真实 OTA 重核价、浏览器 scope 真实 canary、全平台 E2E 均未执行（需用户配对 Companion、授权官方域名并保持登录态）。`icom:transfer` 真实只读公共 API canary 已 PASS，但该单 scope 不构成层 5 通过。

## 下一条可直接执行的命令

```bash
# 0) 用户侧唯一动作：浏览器配对 Companion（Chrome 加载 apps/browser-companion，登录 ctrip/qunar/tongcheng 官方域名）
# 1) 配对完成后先跑 canary，再从头跑六层门：
cd /Users/oxygen/Documents/个人项目/tripchord
uv run python benchmarks/live_canary_certified.py --bridge-token "$(cat .runtime/browser-bridge-token)"
TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit-evidence
```
（层 4 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权模型成本后才会实际运行；层 5 需配对 Companion 且 `ctrip/qunar/tongcheng` 官方域名保持登录态后才会逐 scope 真正 PASS；层 6 在上述条件满足后由 `benchmarks/run_live_done_gate_v4.py` 真实执行。API 已受控重启并绑定新 HEAD=`e862a98`，provenance 三哈希匹配。）

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
