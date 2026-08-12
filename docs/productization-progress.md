# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v1.0 Done-Gate C-122 R39（Block 69–70 监督退回续跑收口）——监督退回的两个硬缺口全部关闭：Block 69 自由文本有界结构包装（`verification code is ((plannerV2))`/`【“plannerV2”】`/`[ plannerV2 ]` 等嵌套+内空白包装经共享 `_registered_base_value_info` 有界去壳回归同一登记 base，叙述与中立 JSON 值双 final fail-closed；预算耗尽/错配/未闭合 `(plannerV2]`/`(plannerV2`/12 层嵌套非法结构外观 fail-closed；正常业务叙述与精确文档路径正例保留。禁止列字符或点名层数——包装按共享有界去壳+结构闭合覆盖，不枚举字符表）；Block 70 Digest 包装剥离预算耗尽 fail-closed（`response=(((((((((deadbeef)))))))))` 9/10/11 层双 final 全拒且 producer 遮蔽，预算耗尽不回落 fullmatch 放行；正常非 hex/算法叙述正例保留）。共享模块 `tripchord._secret_redact` 修复：`_STRUCTURAL_WRAPPER_DEPTH_LIMIT=8` 共享有界常量、`_registered_base_value_info` 有界去壳+预算先于旁路+自配对引号未闭合按 JSON 字符串分隔符、`_EXACT_REGISTERED_BASE_VALUE_ASSIGN_RE` token 整体捕获、`_digest_response_hex_value` 预算耗尽带开层 fail-closed。全量 1008 passed（apps/api/tests + benchmarks/tests）、clean-env 500 passed（scripts/tests，含 R39 新增 3 项）、ruff 全绿。
- **当前分支**：`productization/v1.0`（未 push；API 已在提交 `b673c00` 受控重启并硬校验 provenance 三哈希匹配，pid 5556）
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：C-122 R37 Block 65–66 收口——全量 1008 passed、clean-env 494 passed、ruff 全绿、API 绑定 `8d74bd4`、六层门从头重跑如实记录（层 1/2/3 PASS、层 4 SKIP no model API key authorized、层 5/6 FAIL pending user authorization、passed=false、exit 2、worktree 干净、evidence_commit 未提交），passed=true 前不声称通过

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
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | 受控重启绑定**本提交（round-18 最终 HEAD）**，`/api/v1/agents/runtime` provenance 三哈希匹配、pid 存活（机器证据见 `/api/v1/agents/runtime` 与最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit-evidence` | 从零重跑严格六层门（run_id=`cb38768b287b`），测试 63847b5（round-18 六硬门修复提交，非最终 HEAD）；`passed=false`、`evidence_commit=null`、无 side-channel ref 发布；证据见 `.runtime/done-gate-evidence/gate-20260810T051411Z-cb38768b287b/product-v1-done-gate.json` |

### 第十九轮（C-122，06:58 双重编码绕过修复）验证结果

| 命令 | 结果 |
|---|---|
| `uv run ruff check .` | All checks passed（0 错误） |
| `uv run python scripts/tests/run_tests_clean_env.py scripts/tests/test_run_product_done_gate.py`（clean-env） | 400 passed，退出码 0——含 level 0–4 双重/三重编码三层反例（producer `_desensitize` / consumer `_sanitize_canary_diag_field` / final `_secret_scan_bytes`）、解析失败 fail-closed 反例、深度预算溢出反例、`pending user authorization`/`cookie:` 文本正例边界 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests`（clean-env 全量回归） | 1253 passed，退出码 0（含 scripts/tests + apps/api/tests） |
| `git rev-parse HEAD` | `945b2b0`（本修复提交：新增 `tripchord/_secret_redact.py` + 全链接线 + 回归反例） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`945b2b0`**；`/api/v1/agents/runtime` provenance `commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希与本地树完全匹配、pid 81224（机器证据见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit-evidence` | 从零重跑严格六层门（run_id=`2e21f57d70fe`），tested_commit_sha=`945b2b0`：层 1/2/3/4 PASS、层 5 FAIL（pending user authorization：5 个浏览器 scope 无 Companion 心跳）、层 6 FAIL（pending user authorization / runner `failed_before_done_gate`）；`passed=false` 退出码 2、`evidence_commit=null`、工作树干净；证据 `.runtime/done-gate-evidence/gate-20260810T232057Z-2e21f57d70fe/product-v1-done-gate.json` |

### 第三十六轮（C-122，R36 Block 61–64 监督退回收口）验证结果

| 命令 | 结果 |
|---|---|
| `uv run pytest`（全量） | 1008 passed，退出码 0——含 R36 新增 raw/JSON/全链反例：Block 61 真实 JSON/结构字段路径（quoted-key 统一识别）；Block 62 Digest 增量 fail-closed；Block 63 结构形似伪路径与包裹值（`evil/planner_version`/`evil[planner_version]`/`evil.planner_version` 伪路径、`(plannerV2)`/JSON-escaped 包裹精确 base 于未登记路径，全拒；精确文档路径正例保留）；Block 64 Digest 恢复分支同判（8/16/32/64-hex 未闭合/非法/quoted-torn 尾部全拒，非 hex/无身份叙述不误报） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env 全量） | 491 passed，退出码 0；期间 live `tripchord.db`（274432 B / mtime 1786491483）与 `.runtime/browser-bridge-state.json`（127 B / mtime 1786484531）字节数与 mtime 均不变 |
| `uv run ruff check apps/api/src/tripchord/_secret_redact.py benchmarks/live_canary_certified.py scripts/run_product_done_gate.py scripts/tests/test_run_product_done_gate.py` | All checks passed（0 错误） |
| `git rev-parse HEAD` | `a1c61ec`（R36 Block 61–64 单提交；4 文件 +969/−84；`.probe_tcc.txt` 已删除未纳入交付） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`a1c61ec`**；`/api/v1/agents/runtime` provenance `commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希与本地树完全匹配、pid 1312（`verify_api_runtime_provenance.py` 退出码 0） |
| `uv run python scripts/run_product_done_gate.py --commit a1c61ec --commit-evidence`（scrubbed env：无模型 key、无 `TRIPCHORD_ACK_MODEL_COST`、不配对 Companion） | 从零重跑严格六层门（run_id=`0c38b22841a7`），tested_commit_sha=`a1c61ec`：层 1/2/3 PASS、层 4 SKIP（no model API key authorised in environment; skipped (not failed)）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL（pending user authorization：全平台 E2E 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权后真实执行）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交；证据 `.runtime/done-gate-evidence/gate-20260811T234827Z-0c38b22841a7/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口 |

### 第三十七轮（C-122，R37 Block 65–66 监督退回续跑）验证结果

| 命令 | 结果 |
|---|---|
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests） | 1008 passed，退出码 0（与 R36 同量；R37 代码改动集中在 `tripchord._secret_redact`，apps/api 既有 gate 测试全过） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 494 passed，退出码 0（R36 491 + R37 新增 3 项）——含 R37 新增 raw/JSON/全链反例：Block 65 结构路径/通用配对包装（`evil\planner_version` 反斜杠、`evil::planner_version` 双冒号、`evil/planner_version`、`evil.planner_version`、`evil[planner_version]` 结构分隔伪路径全拒，原则性负类字段续接 `[^\s\"'=]*` 不再枚举分隔符；`verification code is [plannerV2]`/`{plannerV2}`/`<plannerV2>` 通用配对包装、`{"otp": "[plannerV2]"}`/`{"day": "[plannerV2]"}`/`{"otp":"[plannerV2]"}` 中立 JSON 值全拒；未闭合/错配包装 `code is [plannerV2`/`(plannerV2]`/`{"otp": "[plannerV2"}`/`{"otp": "(plannerV2]"}`/`{"otp": "<plannerV2}"}` 非法结构外观 fail-closed；精确文档路径 `planner_version`/`plan.planner_version`/`summary` 含平衡包裹正例与仅提及 base 叙述保留）；Block 66 Digest 恢复同判定（`service Digest username="user", response=deadbeef;`/`response="deadbeef";`/h16/h32 分号尾、malformed 成员 `bad="unterminated, response=…;` 全拒；`client digest note="response=deadbeef", algorithm=md5`/`algorithm="md5 response=deadbeef"`/`algorithm=md5, bad="unterminated, response=<h32>` 算法叙述不误报） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env 全量） | 494 passed，退出码 0；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误） |
| `git rev-parse HEAD` | `8d74bd4`（R37 Block 65–66 单提交；2 文件 +401/−29） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`8d74bd4`**；`verify_api_runtime_provenance.py` provenance `commit_sha`=`8d74bd4`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 33147、mismatches 空 |
| `uv run python scripts/run_product_done_gate.py --commit 8d74bd4 --commit-evidence`（scrubbed env：无模型 key、无 `TRIPCHORD_ACK_MODEL_COST`、不配对 Companion） | 从零重跑严格六层门（run_id=`364eeb1ba2a8`，evidence `gate-20260812T003003Z-364eeb1ba2a8`），tested_commit_sha=`8d74bd4`：层 1/2/3 PASS、层 4 SKIP（no model API key authorised; bounded live model cost not acknowledged）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL（pending user authorization：全平台 E2E 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权后真实执行）；`passed=false` 退出码 2（`--commit-evidence` + passed=false 恒 return 2）、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）；证据 `.runtime/done-gate-evidence/gate-20260812T003003Z-364eeb1ba2a8/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

### 第三十八轮（C-122，R38 Block 67–68 监督 09:15 退回续跑）验证结果

| 命令 | 结果 |
|---|---|
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests） | 1008 passed，退出码 0（与 R37 同量；R38 代码改动集中在 `tripchord._secret_redact`，apps/api 既有 gate 测试全过） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 497 passed，退出码 0（R37 494 + R38 新增 3 项）——含 R38 新增 raw/JSON/全链反例：Block 67 通用 Unicode 配对包装（非 LEFT/RIGHT 命名引号 `“plannerV2”`/`‘plannerV2’`、藏文 `༺plannerV2༻`、欧甘 `᚛plannerV2᚜` 于叙述与中立 JSON 路径双 final fail-closed；`evil"planner_version = …`/`evil=planner_version = …` 包装精确 base 全拒；文档路径 `planner_version`/`plan.planner_version`/`summary` 含包装正例保留；Ps/Pe/Pi/Pf 按 NAME 镜像 + 码点邻接结构闭合，禁止继续补字符表）；Block 68 Digest 恢复同判定（`response=(deadbeef)`/`[deadbeef]`/`( deadbeef )`/`((deadbeef))`/`" deadbeef "`/`“deadbeef”`/`༺deadbeef༻`/`᚛deadbeef᚜`、坏成员 `bad="unterminated, response=…` 恢复与正常解析同 any-non-empty-hex 判定全拒；`client digest note="response=deadbeef", algorithm=md5`/`algorithm="md5 response=deadbeef"`/`response=xyz`/`deadbeefxyz`/`deadbeef.g` 叙述正例不误报） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env 全量） | 497 passed，退出码 0；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误；RUF002/003 对文档化 Unicode 包装字按行 noqa） |
| `git rev-parse HEAD` | `11f244e`（R38 Block 67–68 单提交；2 文件 +437/−34） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`11f244e`**；`verify_api_runtime_provenance.py` provenance `passed=true`、`commit_sha`=`11f244e2bc9042762270facd0ae0a210d8af800e`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 74193、mismatches 空 |
| `uv run python scripts/run_product_done_gate.py --commit 11f244e2bc9042762270facd0ae0a210d8af800e --commit-evidence`（scrubbed env：不设 `TRIPCHORD_ACK_MODEL_COST`、不配对 Companion） | 从零重跑严格六层门（evidence `gate-20260812T014910Z-6110661a3bc9`），tested_commit_sha=`11f244e2bc9042762270facd0ae0a210d8af800e`：层 1/2/3 PASS、层 4 SKIP（model key present but bounded live model cost not acknowledged；不设 `TRIPCHORD_ACK_MODEL_COST=1`）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL（pending user authorization：全平台 E2E 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权后真实执行）；`passed=false` 退出码 2（`--commit-evidence` + passed=false 恒 return 2）、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）；证据 `.runtime/done-gate-evidence/gate-20260812T014910Z-6110661a3bc9/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

### 第三十九轮（C-122，R39 Block 69–70 监督退回续跑）验证结果

| 命令 | 结果 |
|---|---|
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests） | 1008 passed，退出码 0（与 R38 同量；R39 代码改动集中在共享模块 `tripchord._secret_redact`，apps/api 既有 gate 测试全过） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 500 passed，退出码 0（R38 497 + R39 新增 3 项）——含 R39 新增 raw/JSON/全链反例：Block 69 自由文本有界结构包装（`verification code is ((plannerV2))`/`【“plannerV2”】`/`[ plannerV2 ]` 嵌套+内空白包装经共享有界解析回归同一登记 base，双 final fail-closed；预算耗尽/错配/未闭合 `(plannerV2]`/`(plannerV2`/12 层嵌套全拒；正常业务叙述与文档路径正例保留。禁止列字符或点名层数——包装按共享 `_registered_base_value_info` 有界去壳+结构闭合覆盖，不枚举字符表）；Block 70 Digest 包装剥离预算耗尽 fail-closed（`response=(((((((((deadbeef)))))))))` 9/10/11 层双 final 全拒且 producer 遮蔽，预算耗尽不回落 fullmatch 放行；正常非 hex/算法叙述正例保留） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env 全量） | 500 passed，退出码 0；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误） |
| `git rev-parse HEAD` | `b673c00`（R39 Block 69–70 单提交；2 文件 +289/−36） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`b673c00`**；`verify_api_runtime_provenance.py` provenance `passed=true`、`commit_sha`=`b673c00b22effcb942fb0c6474ffb711dd722770`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 5556、mismatches 空 |
| `uv run python scripts/run_product_done_gate.py --commit b673c00b22effcb942fb0c6474ffb711dd722770 --commit-evidence`（scrubbed env：不设 `TRIPCHORD_ACK_MODEL_COST`、不配对 Companion） | 从零重跑严格六层门（run_id=`99e32f9002e5`，evidence `gate-20260812T033748Z-99e32f9002e5`），tested_commit_sha=`b673c00`：层 1/2/3 PASS、层 4 SKIP（model key present but bounded live model cost not acknowledged；不设 `TRIPCHORD_ACK_MODEL_COST=1`）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL（pending user authorization：全平台 E2E 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权后真实执行）；`passed=false` 退出码 2（`--commit-evidence` + passed=false 恒 return 2）、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）；证据 `.runtime/done-gate-evidence/gate-20260812T033748Z-99e32f9002e5/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

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
（层 4 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权模型成本后才会实际运行；层 5 需配对 Companion 且 `ctrip/qunar/tongcheng` 官方域名保持登录态后才会逐 scope 真正 PASS；层 6 在上述条件满足后由 `benchmarks/run_live_done_gate_v4.py` 真实执行。API 已受控重启并绑定新 HEAD=`b673c00`（C-122 R39 Block 69–70 收口提交），provenance 三哈希匹配。）

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
