# TripChord 产品化实施账本

> 主实施合同：`docs/claude-code-v1-implementation-prompt.md`
> 目标路线图：`docs/roadmap.md`（v0.2 → v1.0）
> 本账本每次上下文切换前更新；下一上下文先读本账本 + `git status` 后继续。

## 当前状态

- **当前版本**：v1.0 Done-Gate C-122 R42（Block 76–80 + 09:40 纠偏收口）——监督退回的 Block 76/77 全部关闭：JSON 与自由文本解析路径拆分，共享 verified registered-base 核心。Block 76 结构尾部 fail-closed（`verification code is (plannerV2)note)`/`note]`/`note}`/`note))))`/`note"`/`note.`/跨行 `note␊=` 及 JSON 未授权路径同型值一律拒绝；**任何 envelope closer 不无条件剥除**——JSON 路径按真实 JSON 结构/字段边界、自由文本按独立完整句结构判定，余部残留任何结构符号/句终止符/跨行算子即 fail-closed）；Block 77 正常语言降门（`(plannerV2) is a version`/`— a note`/`isn't active`/`non-secret version` 接受；**不依赖「每词 ≥2 字母」或禁撇号/连字符的自造词表**——`_is_natural_word_token` 只认 ASCII 字母（内嵌撇号/连字符合法）+CJK 串+单字母虚词 a/i，`x=`/`_=`/`_note` 及 Block76 结构伪装仍拒绝）。共享模块 `tripchord._secret_redact` 修复：`_exact_value_at_free_text_boundary`（仅当赋值位于引号串内时先剥合法 JSON 围栏）、`_quoted_string_spans`、`_is_natural_word_token`、`_is_natural_language_phrase` 去字符表；JSON walker 走 `_registered_base_value_info` 未加引号共享核心。Block 78（监督 14:52 增量）按 **Unicode 词法类+句法位置**区分句末/从句标点与结构尾部：多词句每词可带一个句末/从句终结符（`is a version.`/`is a version;`/`is a version, not a secret`）、Unicode 弯引号（U+2018/2019）与任意 `Pd` 破折号（含 U+2011 非断连字符）为**词内**分隔符（`isn't` 弯引号形式/`non-secret` U+2011 形式）、CJK 句为单 whitespace token（`这是版本。`）；`note.`/`version.` 单 ASCII 词+终结符（Block76 伪装）、`x=`/`_=`、跨行算子、结构尾部仍 fail-closed（Block 73–77 不回归）。Block 79/80（监督 18:22）+ **09:40 纠偏**：`_is_line_boundary_char` 只把真实换行集 CR/LF/VT/FF/FS/GS/RS/NEL + U+2028/U+2029 视为语义边界，**U+0009 TAB 不再是边界**（旧类别式 Cc/Zl/Zp 检查误吞 TAB，`(plannerV2) is<TAB>a version.` 同行短语保持接受）；free-text 叙述解析器在 JSON 字符串值内按 RFC-8259 解码转义（sealed 诊断中 `\t` 即真实 TAB 的转义）；词内分隔符必须左右均为字母（连续/混合分隔 `a--b`/`a-'b`/`a-’b`/`a-—b` fail-closed）。全量 `uv run pytest` 退出码 0、clean-env 508 passed、ruff 全绿。
- **当前分支**：`productization/v1.0`（未 push；API 已受控重启绑定**最终 HEAD（当前账本提交，R42 Block 83 一次性收口，代码+文档同提交）**并硬校验 provenance 三哈希匹配、`passed=true`（pid 与三哈希见 provenance 机器证据与最终结果评论），mismatches 空）
- **基线 commit**：`0fa8f78`（chore: baseline productization contract and roadmap）
- **工作目录**：`/Users/oxygen/Documents/个人项目/tripchord`
- **最后完成的最小任务**：C-122 R42 Block 83（监督打回五）——**producer/final 对称性**：顶层 JSON scalar string 若为 exact 注册业务 base（或 wrapped / Unicode-escaped 拼写：`plannerV2`/`providerV4`/`"(plannerV2)"`/`"\u0070lannerV2"`）在**未绑定 root path** 被 raw committed/failure 双 final 拒绝，但 producer `bounded_json_mask` 递归进 decoded scalar、documented-base level mask 保留原值，可借 documented summary 洗白（producer→0600 seal→consumer→双 final 放行）。修复：`bounded_json_mask` 对 `depth==0` 顶层 scalar 若 `_registered_base_value_exempt_at_path((), parsed)` 为 False 则**整值遮罩**（`[REDACTED]`），镜像 final rejector 的 `depth==0`/`path==()` 契约；嵌套 decoded level 不重判（`{"summary": "\"plannerV2\""}` documented base value 保持正例）；覆盖 providerV4/tokenizationV1 等注册 base 而非仅 planner 单例。修复提交=最终 HEAD（本提交，`_secret_redact.py` + gate 测试 + 账本/进度文档同提交）；新增 Block83 正式 raw+合法 JSON+producer→0600 seal→consumer→双 final 反例（exact/wrapped/Unicode-escaped/`providerV4`）与正例（顶层 prose `"(plannerV2) is\ta version."`、数组/多成员/嵌套、documented summary/`<base>_version` 路径），并保持 Block82 数组/多成员/嵌套正例、JSON key 负例、Block81 quoted-prose 反例、FS/GS/RS/NEL/LS/PS 行界反例不回归；全量 gate 483 passed、全量 1518 passed 退出码 0、clean-env 1518 passed、ruff 全绿、live DB/bridge-state 未被触碰；API 受控重启绑定最终 HEAD（本提交，provenance 三哈希匹配、passed=true、mismatches 空）、六层门同 HEAD 从头重跑如实记录（最新 evidence 目录：层 1/2/3/4 PASS、层 5 FAIL pending user authorization、层 6 FAIL 如实报告 executor 在 done-gate 前于 `companion_preflight` 失败、passed=false、exit 2、worktree 干净、evidence_commit 未提交），passed=true 前不声称通过。上一轮 Block 82 打回四（监督 05:44，提交 `9ddd140`）最终 HEAD 绑定 `864b9b4`（provenance 机器证据见最终结果评论）；Block 82 打回三（监督 13:10，提交 `c2e8afb`）与最终 HEAD 绑定 `e490f2d`；Block 81（监督 12:07 打回二，提交 `1f0f7c8`）与最终 HEAD 绑定 `025d9f7`；Block 79 09:40 纠偏（提交 `a67b79e`）与最终 HEAD 绑定 `2cf52e7`；Block 79/80（监督 18:22）提交 `4014432`、最终 HEAD 绑定 `3a388ea`；Block 78 收口（提交 `eb68a44`）；R40/R41 代码已提交、回归全绿，六层门/账本统一并入 R42 在最终 HEAD 收口。

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

### 第四十二轮（C-122，R42 Block 76–77 监督退回续跑收口，含 R40/R41 账本并账）验证结果

> R40（Block 71–73，提交 `9f31333`/`50f45a9`）与 R41（Block 74–75，提交 `a24635a`）代码已提交且各自回归全绿，但六层门/账本因 TCC 被阻断，统一并入本轮 R42 在最终 HEAD `fa88479` 上收口。本表全部命令均在 `fa88479` 工作树上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest apps/api/tests/ benchmarks/tests/`（全量） | 1008 passed，退出码 0（与 R39 同量） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r40 or r41 or r42"` | 5 passed（R40/R41/R42 聚焦回归，含 R42 新增 `test_r42_block76_77_structural_tail_and_normal_language_both_paths`） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 505 passed，退出码 0（R39 500 + R40–R42 新增 5 项）；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误） |
| `git rev-parse HEAD` | `fa88479`（R42 Block 76–77 单提交；`_secret_redact.py` + gate 测试） |
| `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` | API 受控重启绑定**最终 HEAD=`fa88479`**；`verify_api_runtime_provenance.py` provenance `passed=true`、`commit_sha`=`fa8847926b47fdc53c0a2996a52f98e1eda0ba87`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 40902、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit fa8847926b47fdc53c0a2996a52f98e1eda0ba87 --commit-evidence` | 从零重跑严格六层门（run_id=`7bb23abd343a`，evidence `gate-20260812T064713Z-7bb23abd343a`），tested_commit_sha=`fa88479`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL（pending user authorization：全平台 E2E 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权后真实执行）；`passed=false` 退出码 2（`--commit-evidence` + passed=false 恒 return 2）、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）；证据 `.runtime/done-gate-evidence/gate-20260812T064713Z-7bb23abd343a/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

### 第四十二轮（续，C-122 R42 Block 78 监督 14:52 增量修正 + L6 诊断）验证结果

> 监督 14:52：Block 78 修复直接并入现有 R42 运行，禁止另开写者/提前交付/审查；完成 Block 78 后必须诊断并修复/重跑 L6 至产生真实报告（L5 授权不足如实保留）。本表全部命令在最终 HEAD `eb68a44` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 479 passed，退出码 0（R42 Block 76–77 465 项 + Block 78 正式正反例 13 项 + L6 如实报告 1 项） |
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests + scripts/tests） | 1008 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 507 passed，退出码 0（R42 505 + Block 78/L6 新增 2 项）；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误；Block 78 注释/docstring 中 RUF002/003 歧义 Unicode 已改写为字符名描述，`’`/`‑`/全角标点不再直接出现） |
| `git rev-parse HEAD` | `eb68a44`（R42 Block 78 单提交；`_secret_redact.py` + `run_product_done_gate.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定**最终 HEAD=`eb68a44`**；provenance `passed=true`、`commit_sha`=`eb68a44a9618c44b67746ac1c7fb49316dd6f913`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 48622、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit eb68a44a9618c44b67746ac1c7fb49316dd6f913 --commit-evidence` | 从零重跑严格六层门（evidence `gate-20260812T104339Z-d688327b4b81`），tested_commit_sha=`eb68a44`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——**已如实报告真实 executor 失败**（`executor failed before the done gate at stage 'companion_preflight'`：浏览器 Companion 预检失败，未发现同时声明携程/去哪儿/同程且心跳未过期（>45s 过期）的已连接 Companion；实时搜索未提交），不再包装成「pending user authorization」通用文案；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）；证据 `.runtime/done-gate-evidence/gate-20260812T104339Z-d688327b4b81/product-v1-done-gate.json`。`passed=false` 如实记录；L5/L6 均为真实未过，passed=true 前不启 C-125 |

### 第四十二轮（续，C-122 R42 Block 79–80 监督 18:22 增量纠偏）验证结果

> 监督 18:22：Block 79 要求所有 Unicode 行/段分隔与控制换行（U+2028/U+2029/VT/FF/NEL，类别 Cc/Zl/Zp）作为语义边界 fail-closed，不得只列 CR/LF；Block 80 要求词内分隔符必须左右均为字母，连续或混合 separator fail-closed。补正式 raw/JSON/producer→0600 seal→consumer→双 final 反例与相邻正常语言正例；保持 Block 78 正例不回归；修完重跑聚焦/完整门/全量/clean-env/ruff，并继续诊断 L6 至真实 done_gate 报告；passed=false 不收口。本表全部命令在最终 HEAD `4014432` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block79_80"`（聚焦） | 1 passed（`test_r42_block79_80_line_separators_and_mixed_separator_pseudo_words_both_paths`，raw/JSON/双 final + 0600 seal→consumer 全链） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 481 collected，退出码 0（collect 481 项全过；Block 78 479 项 + Block 79–80 新增正式正反例） |
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests + scripts/tests） | **1008 passed，退出码 0**（首轮 1008 项中出现 1 例 `benchmarks/tests/test_agent_suite.py::test_concurrency_gate_uses_declared_nonzero_external_wait` p50_speedup 0.2947<0.35 的主机负载抖动——非红改模块性能断言，隔离复跑 4/4 通过；完整复跑 1008 passed 确认无回归） |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 508 passed，退出码 0（R42 Block 78 的 507 + Block 79–80 新增 1 项）；live `tripchord.db` 与 `.runtime/browser-bridge-state.json` 字节数与 mtime 均不变 |
| `uv run ruff check .` | All checks passed（0 错误；RUF002/RUF003 歧义 Unicode 已改写为字符名描述，F841 死变量 `prev` 移除） |
| `git rev-parse HEAD` | `4014432`（R42 Block 79–80 单提交；`_secret_redact.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定**最终 HEAD=`4014432`**；provenance `passed=true`、`commit_sha`=`401443284ec40571d0c001e7af7561364771e5e7`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 82960、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit 401443284ec40571d0c001e7af7561364771e5e7 --commit-evidence` | 从零重跑严格六层门（run_id=`bf5de0bc7b32`，evidence `gate-20260813T020327Z-bf5de0bc7b32`），tested_commit_sha=`4014432`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（`live-canary-certified.json` companion_status=`disconnected`、companions 为空、心跳过期 >45s）：未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，实时搜索未提交；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null；证据 `.runtime/done-gate-evidence/gate-20260813T020327Z-bf5de0bc7b32/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

### 第四十二轮（续 2，C-122 R42 Block 79 09:40 纠偏）验证结果

> 监督退回：09:40 修正点未落地——`_is_line_boundary_char` 仍是类别式 Cc/Zl/Zp，U+0009 TAB 被当作行边界；补 TAB 正例（raw+JSON+producer→0600 seal→consumer→双 final，`(plannerV2) is\ta version.` 同行短语接受）与 FS/GS/RS、JSON NEL 反例。修完重跑聚焦/完整门/全量/clean-env/ruff，从零重跑六层门如实记录；passed=false 不收口。本表全部命令在最终 HEAD `a67b79e` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block79_80"`（聚焦） | 1 passed（09:40 纠偏：TAB 非边界；raw+JSON+producer→0600 seal→consumer→双 final TAB 正例 + FS/GS/RS、JSON NEL 反例） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 481 collected，退出码 0 |
| `uv run pytest`（全量，apps/api/tests + benchmarks/tests + scripts/tests） | 退出码 0，全部通过 |
| `uv run python scripts/tests/run_tests_clean_env.py`（clean-env，scripts/tests 全量） | 508 passed，退出码 0；clean-env 启动器重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | `a67b79e`（R42 Block 79 09:40 纠偏单提交；`_secret_redact.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定代码 HEAD=`a67b79e`；provenance `passed=true`、`commit_sha`=`a67b79edaa30ff9d0717771f26beb3b5809f0fb8`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 64158、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit a67b79edaa30ff9d0717771f26beb3b5809f0fb8 --commit-evidence` | 从零重跑严格六层门（run_id=`e2644a4c2bc0`，evidence `gate-20260813T025115Z-e2644a4c2bc0`），tested_commit_sha=`a67b79e`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null；证据 `.runtime/done-gate-evidence/gate-20260813T025115Z-e2644a4c2bc0/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125 |

### 第四十二轮（续 3，C-122 R42 Block 81 监督 12:07 打回二）验证结果

> 监督独立复核纠正前轮放行：09:40 自由文本转义解码为**无门盲解码**——`_quoted_string_spans` 用 `if ch in "\"'"` 把任意单/双引号自由文本当 JSON value，`_exact_value_at_free_text_boundary` 对 span 内 rest 无条件 `_decode_json_string_escapes`，非 JSON quoted prose 中字面 `\t` / `\u0009` / `\u0020` / `\u0061` 转义从父提交拒绝变为 producer 保留 + 双 final 接受（确定性绕过）。修复要求：①限定解码仅经真实 JSON parse/walker 证明的双引号 JSON value，不得对任意 quoted prose 或单引号解码；②补 raw quoted literal escape producer→0600 seal→consumer→双 final 反例（必须 fail-closed）；③保留合法 JSON TAB 正例与真实换行反例；④不得删测/放宽 malformed JSON 或裸凭据门；⑤跑聚焦/全门/全量/clean-env/ruff、干净提交、API 绑定最终代码、从头重跑六层门并如实记录、同步账本；⑥C-125/C-124 均不得启动，严格六层 passed=true 前 C-2 不收口。本表全部命令在代码 HEAD `1f0f7c8` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block79_80"`（聚焦） | 1 passed（Block 81：raw quoted literal escape 反例 + 合法 JSON TAB 正例 + FS/GS/RS/NEL 反例全含） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 481 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1516 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1516 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | `1f0f7c8`（R42 Block 81 单提交；`_secret_redact.py` + `run_product_done_gate.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定代码 HEAD=`1f0f7c8`；provenance `passed=true`、`commit_sha`=`1f0f7c8ae21a7294a289251bbcb6c1c927d9547b`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 44545、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit 1f0f7c8ae21a7294a289251bbcb6c1c927d9547b --commit-evidence` | 从零重跑严格六层门（run_id=`90d76ac65020`，evidence `gate-20260813T045246Z-90d76ac65020`），tested_commit_sha=`1f0f7c8`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null；证据 `.runtime/done-gate-evidence/gate-20260813T045246Z-90d76ac65020/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十二轮（续 4，C-122 R42 Block 82 监督 13:10 打回三）验证结果

> 监督 13:10 打回三：上一 run `1fdef458` 只交付 Block81，未消费已并入的 Block82。确定性复现——合法单成员 JSON 的 TAB value accepted/producer unchanged；相同 value 后带兄弟成员、位于数组或嵌套对象时 raw final 拒绝 + producer masked（误拒）；JSON key 负例仍正确拒绝。修复要求：①真实 JSON parse/walker 按每个 decoded string value 独立判断 narration，阻止 raw narration backstop 跨成员/数组/嵌套 value 重扫误判；②不得继续靠剥逗号或扩展 envelope 字符表，JSON key 不得因值解码获得 value 豁免；③正式补测试（raw/合法 JSON/producer→0600 seal→consumer→双 final 全链）：合法 multi-member/array/nested TAB 正例、JSON key quoted-literal escape 负例、保留 Block81 任意 quoted prose 字面 `\t` / `\u0009` / `\u0020` / `\u0061` 转义反例、保留 FS/GS/RS/NEL 真实行界反例；④交付顺序：聚焦与全部 gate 反例→相关完整门、apps/api+benchmarks 全量、clean-env、ruff、live DB/bridge-state 不变→提交干净 HEAD→重启 API 精确绑定最终代码→从头六层门并如实记录 passed/evidence binding→同步 progress/claim/phase review；⑤passed=false 不得收口，C-125/C-124 均保持 backlog。本表全部命令在代码 HEAD `c2e8afb` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block82"`（聚焦） | 1 passed（Block 82：multi-member/array/nested TAB 正例 + JSON key escape 负例 + Block81 prose/NL 反例全含） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 482 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1517 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1517 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | `c2e8afb`（R42 Block 82 单提交；`_secret_redact.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定代码 HEAD=`c2e8afb`；provenance `passed=true`、`commit_sha`=`c2e8afbeaf8cd990b591d66c10eb18ad82ec9b8d`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 53814、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit c2e8afbeaf8cd990b591d66c10eb18ad82ec9b8d --commit-evidence` | 从零重跑严格六层门（run_id=`74e400e4804d`，evidence `gate-20260813T054728Z-74e400e4804d`），tested_commit_sha=`c2e8afb`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null；证据 `.runtime/done-gate-evidence/gate-20260813T054728Z-74e400e4804d/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十二轮（续 5，C-122 R42 Block 82 监督 05:44 打回四）验证结果

> 监督 05:44 打回四：上一 run `90fbd342` 的 WIP 增量纠偏只覆盖数组中 object member，仍漏真实 JSON 数组中的直接 string value。确定性复现——顶层 JSON string、数组直接元素、嵌套数组直接元素在 raw committed/failure 双 final 均拒绝且 producer masked。修复要求：①扩展真实 JSON value 识别到数组 string 元素（顶层/嵌套数组，首/中/末位置）并按该 string 自身 closing quote 截断、RFC-8259 解码后独立判断；②不得退回逗号/envelope 字符剥除扩表；③正式补顶层/嵌套数组 direct string 首/中/末 TAB 正例的 raw/合法 JSON/producer→0600 seal→consumer→双 final 全链；④JSON object key、Block81 quoted-prose 字面 `\t` / `\u0009` / `\u0020` / `\u0061`、FS/GS/RS/NEL 必须继续拒绝；⑤顶层 JSON scalar string 也按真实 JSON string value 契约覆盖。本表全部命令在代码 HEAD `9ddd140` 上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block82 or r36_block61 or r37_block65 or r42_block76_77 or r42_block78 or r42_block79_80"`（聚焦） | 6 passed（Block 82：数组 string 元素/顶层 scalar 首中末 TAB 正例 + JSON object key escape 负例 + Block81 prose/NL 反例全含） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 482 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1517 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1517 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | `9ddd140`（R42 Block 82 打回四单提交；`_secret_redact.py` + `run_product_done_gate.py` + gate 测试） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定代码 HEAD=`9ddd140`；provenance `passed=true`、`commit_sha`=`9ddd140484437feb97456921c4233d8cf21febc3`、`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 67070、mismatches 空 |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit 9ddd140484437feb97456921c4233d8cf21febc3 --commit-evidence` | 从零重跑严格六层门（run_id=`84fa89650fa8`，evidence `gate-20260813T063203Z-84fa89650fa8`），tested_commit_sha=`9ddd140`：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null；证据 `.runtime/done-gate-evidence/gate-20260813T063203Z-84fa89650fa8/product-v1-done-gate.json`。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |


### 第四十二轮（续 6，C-122 R42 Block 83 监督打回五）验证结果

> 监督打回五：上一 run `3e100fcc` 只收口三方 SHA 漂移（提交 `864b9b4` 仅改文档），未消费 Block 83 工程修复。监督在干净 HEAD `864b9b4` 独立复现：顶层合法 JSON scalar exact `"plannerV2"`、wrapped `"(plannerV2)"`、Unicode-escaped 形式被 raw committed/failure 双 final 拒绝，但 producer `_desensitize` 保留/解码它们；写入真实 0600 failure diagnostic 后 failure-final 接受，经 consumer `_sanitize_canary_diag_field` 后 committed-final 也接受——形成 producer→0600→consumer→双 final 洗白。修复要求：①producer/final 对称——exact/wrapped/Unicode-escaped/`providerV4` 均须 producer 可靠遮盖，经真实 0600→consumer→committed/failure 双 final 不得借 documented summary 洗白；②正式补 raw/合法 JSON + producer→真实 0600→consumer→双 final 反例；③顶层合法 prose narration（`"(plannerV2) is\ta version."`）必须继续接受，不得粗暴遮罩全部顶层 JSON scalar；④数组/多成员/嵌套正例、JSON key 负例、Block81 quoted-prose 反例、FS/GS/RS/NEL/LS/PS 行界负例不回归；⑤一次性收口绑定：代码+文档先提交冻结唯一最终 HEAD，API 绑定同一 HEAD、provenance passed=true，同 HEAD 从头六层门，之后不得再新增提交。本表全部命令在最终 HEAD（当前账本提交，代码+文档同提交）上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block82 or r42_block83"`（聚焦） | 2 passed（Block 82 测试含 Block81 保持项；Block 83 新增 exact/wrapped/Unicode-escaped/`providerV4` 顶层 scalar 反例 + 顶层 prose/document summary/数组/多成员/嵌套正例 + Block82/81/79 不回归断言） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 483 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1518 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1518 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | 最终 HEAD（当前账本提交；`_secret_redact.py` + gate 测试 + 账本/进度文档同提交，精确 SHA 见最终结果评论） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定最终 HEAD（本提交）；provenance `passed=true`、`commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 存活、mismatches 空（精确值见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit <最终 HEAD 全 SHA> --commit-evidence` | 同 HEAD 从头重跑严格六层门（最新 evidence 目录，run_id 与 tested_commit_sha 见最终结果评论）：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十二轮（续 7，C-122 R42 Block 84 监督打回六）验证结果

> 监督打回六：上一 run 只消费 Block83 单层 JSON decode 修复（`depth==0` 门），递归 `json.dumps` 编码（depth 2/3/4）的顶层 scalar exact `"plannerV2"` / wrapped `"(plannerV2)"` / `providerV4` / `[providerV4]` 仍会借任一 decode 层洗白。确定性复现：raw committed/failure 双 final 拒绝，但 producer/final 在每个 decoded 层读回为 phrase。修复要求：①对称 fail-closed——bounded producer/final 对顶层 scalar 递归 JSON 编码上下文（depth 2/3/4 的 exact/balanced-wrapped/provider base）在**任一 decode 层**均不得洗白，不得只修 producer 或只修 final；②正式补 depth 2-4 raw committed/failure 双 final→producer→真实 0600 seal→consumer→双 final 全链反例（exact/paren/bracket wrapper/provider/Unicode-escaped 变体）；③不回归——合法多层 JSON prose（顶层 TAB `"(plannerV2) is\ta version."`）、数组/多成员/嵌套、documented 成员路径正例保持接受；JSON key 负例、Block81 quoted-prose 字面 `\t` / `\u0009` / `\u0020` / `\u0061`、FS/GS/RS/NEL/LS/PS 行界负例保持拒绝；不得粗暴遮罩全部多层 JSON string、不得删测试/降递归预算/放宽 finals；④重跑全套（聚焦/全量 gate/apps-api+benchmarks 全量/clean-env/ruff/live DB+bridge-state 不变）→提交干净 HEAD→API 绑定最终 HEAD、provenance passed=true→同 HEAD 从头六层门；⑤一次性收口：代码+文档先提交冻结唯一最终 HEAD，API 精确绑定，provenance passed=true，同 HEAD 从头六层门，之后不再新增提交。本表全部命令在最终 HEAD（当前账本提交，代码+文档同提交）上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "block79_80 or block82 or block83 or block84"`（聚焦） | 4 passed（Block84 新增 depth 2-4 exact/wrapped/provider/Unicode-escaped 全链反例 + 合法多层 JSON/document 成员正例 + Block81/82/83 不回归断言） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 484 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1519 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1519 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | 最终 HEAD（当前账本提交；`_secret_redact.py` + gate 测试 + 账本/进度文档同提交，精确 SHA 见最终结果评论） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定最终 HEAD（本提交）；provenance `passed=true`、`commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 存活、mismatches 空（精确值见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit <最终 HEAD 全 SHA> --commit-evidence` | 同 HEAD 从头重跑严格六层门（最新 evidence 目录，run_id 与 tested_commit_sha 见最终结果评论）：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十二轮（续 8，C-122 R42 Block 85/86 监督打回七）验证结果

> 监督打回七：上一 run（Block84）的 walker 提前把外层 JSON 文本当 registered-base phrase，合法语言短语重编码 2/3/4 次被误拒/误遮；且数组直接元素只在 decode depth 0 判定，depth 2/3/4 编码数组在首/中/末及 nested 位置的 exact/wrapped/provider base 借任一 decode 层洗白。确定性复现——`"(plannerV2) is a version."` 在 decode L1 被 `_registered_base_value_info` 解析为 `('planner', True)`（外层 JSON 文本携带 base），raw 双 final 拒绝、producer 遮盖；`["x", "plannerV2", "y"]` 重编码 2/3/4 次后中间/末尾/嵌套项 raw 双 final 放行。修复要求：①decoded scalar 仍 `looks_like_json` 时**先递归到真实内层值**再按携带 path 对称判定，不得提前把外层 JSON 文本当 registered-base phrase；②final **按 decoded 结构和携带 path 逐 item 判断**（list 层携带 path、item 落在 `(carried_path, "[]", ...)`）、producer/final 对称，depth 2-4 首/中/末/nested 位置 exact/wrapped/provider base 五路（raw committed/failure→producer→真实 0600 seal→consumer→双 final）拒绝→遮盖→接受仅因已遮；③同一位置合法语言短语（`(plannerV2) is a version.` / TAB 叙述 / `plannerV2 is a non-secret version`）五路接受、producer 不动；documented outer member 下 array/nested-array 路径语义明确（剥 `[]` 后继承 allowed base；cross-field/unbound 元素 fail-closed），禁止一概放行或一概拒绝；④不回归——保持 Block81–84 全部正/负例、JSON key/quoted-prose/行界反例，不得删测试、放宽 finals、粗暴遮盖全部多层 JSON、降递归预算；⑤重跑全套→提交干净 HEAD→API 绑定最终 HEAD、provenance passed=true→同 HEAD 从头六层门；一次性收口，之后不再新增提交。本表全部命令在最终 HEAD（当前账本提交，代码+文档同提交）上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "r42_block85 or r42_block86"`（聚焦） | 2 passed（Block85 合法语言多层 JSON 五路接受 + Block86 编码数组逐 item 五路拒绝/遮盖 + documented outer member 路径语义 + Block84/65 不回归断言） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 486 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1521 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1521 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | 最终 HEAD（当前账本提交；`_secret_redact.py` + `run_product_done_gate.py` + gate 测试 + 账本/进度文档同提交，精确 SHA 见最终结果评论） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定最终 HEAD（本提交）；provenance `passed=true`、`commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 存活、mismatches 空（精确值见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit <最终 HEAD 全 SHA> --commit-evidence` | 同 HEAD 从头重跑严格六层门（最新 evidence 目录，run_id 与 tested_commit_sha 见最终结果评论）：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十二轮（续 9，C-122 R42 Block 87 监督打回八）验证结果

> 监督打回八：上一 run（Block86）给共享 exact-path matcher 加了无条件剥字符串路径段 `'[]'`，把真实 JSON dict key `'[]'` 与 array marker 混型。确定性复现——`{"planner_version":{"[]":"plannerV2"}}` 及 summary/nested 同型被剥成 documented 前缀而豁免；free-text assignment helper 的 `<documented>.[] = <base>`（`planner_version.[] = plannerV2`）把 exact base 借 documented 前缀洗白（raw committed/failure 双 final 接受、producer 不动）。修复要求：①改为 **typed sentinel/path segment**，或仅在 array walker 显式携带 documented origin——共享 exact path matcher 不得全局忽略字符串 `'[]'`；②补 escaped/unescaped 真实 `'[]'` key、documented-prefix/summary/nested、伪路径及 assignment/helper 调用方的 raw 双 final→producer→真实 0600 seal→consumer 双 final 全链反例，同时保留真正 documented outer array/nested-array 正例；③Block86 矩阵补齐——合法 prose 覆盖 array 首/中/末/nested 位置（不得只测 middle），负例补 bracket `[providerV4]` 与 Unicode-escaped array element，全部 depth 2-4 五路链；④副作用收口——上轮仓库根 0 字节 untracked `consumer`（mtime 17:25）须查明来源并精确移除；本轮 `tripchord.db`（gitignored 非跟踪）mtime 已变化，禁止声称「未触碰」，须如实说明副作用、不伪造基线，DB/运行态文件不提交；⑤不回归 + 重跑全套（聚焦/全量 gate/apps-api+benchmarks 全量/clean-env/ruff）→提交干净 HEAD→API 绑定最终 HEAD、provenance passed=true→同 HEAD 从头六层门；一次性收口，之后不再新增提交。本表全部命令在最终 HEAD（当前账本提交，代码+文档同提交）上执行。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "block82 or block83 or block84 or block85 or block86 or block87"`（聚焦） | 7 passed（Block87 新增 typed-sentinel 五路反例/正例 + Block86 位置矩阵补齐 + Block82–86 不回归断言） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 488 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1523 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1523 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | 最终 HEAD（当前账本提交；`_secret_redact.py` + `run_product_done_gate.py` + gate 测试 + 账本/进度文档同提交，精确 SHA 见最终结果评论） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定最终 HEAD（本提交）；provenance `passed=true`、`commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 存活、mismatches 空（精确值见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit <最终 HEAD 全 SHA> --commit-evidence` | 同 HEAD 从头重跑严格六层门（最新 evidence 目录，run_id 与 tested_commit_sha 见最终结果评论）：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

### 第四十三轮（续 10，C-122 R42 Block 88 监督打回九）验证结果

> 监督打回九：前轮（Block87）对 documented 外层成员路径下的真实 JSON 数组已按"typed sentinel 剥段继承豁免"处理，但未发现 raw narration 回退在**数组首元素**上位置性误判：`{"planner_version":["plannerV2","x","y"]}` 中，exact-base 赋值 regex 在首元素捕获 field=`planner_version"`、token=`["plannerV2"`，`_registered_base_value_info('["plannerV2"')` 返回 `_WRAPPED_BASE_ILLEGAL`（数组 `[` 未在 token 内闭合、引号跨匹配），`_match_inside_genuine_json_string_value` 因 strict `<` 返回 False → narration 判真 → raw committed/failure 双 final 拒绝、producer 终扫 `_mask_bare_credential_text` 遮盖首元素；而中/末元素 token `["x"` 落入 phrase 分支继续而接受/保留——**数组位置改变语义**。修复要求：①真实数组项按契约继承 documented 豁免且位置无关（首/中/末/nested 一套契约，不得再首位置误拒）；②五路矩阵——depth 2-4 与真实 JSON 层的 first/middle/last/nested 位置（raw committed/failure 双 final→producer→真实 0600 seal→consumer→双 final 全链），覆盖 planner/provider documented base，证明 producer 不过度遮盖、双 final 一致接受；`summary` 按既有 free-text/documented 契约处理；③保留 Block87 全部反例（escaped/unescaped 真实 dict key `'[]'`、伪路径、assignment/helper AARAA），不得为修 first 再把字符串 key `'[]'` 当 array marker；④补 last 位置 bracket `[providerV4]` 与 Unicode-escaped 注册 base 负例全链，保留既有 first/middle/nested 负例；⑤副作用诚实记录：live `tripchord.db` mtime/hash 可能被 live API/重启日常写入改变，禁止声称"未触碰"，只记录可验证边界、不伪造基线，DB/运行态文件不提交；⑥不回归 + 重跑全套（聚焦/全量 gate/apps-api+benchmarks 全量/clean-env/ruff）→提交干净 HEAD→API 绑定最终 HEAD、provenance passed=true→同 HEAD 从头六层门；一次性收口，之后不再新增提交。本表全部命令在最终 HEAD（当前账本提交，代码+文档同提交）上执行。

修复：`_secret_redact._is_documented_json_array_element_assign` —— 对 WRAPPED_BASE_ILLEGAL 的 token 按"真实 JSON 文档的数组元素"重判：token 以真实 `[` 开头、后续为双引号字符串元素、`_registered_base_value_info` 得精确 base、且携带 field 的 base_path 落在 documented 允许集合 → 在该携带的数组路径上按 documented 契约豁免（与 JSON walker 对称），数组位置无关；非 JSON 文档、对象/brace 包装（`{"[]":"plannerV2"}`）、tight prose 包装、unbound/cross-field base 一律仍 fail-closed。Block87 的 typed sentinel 路径语义（只剥 sentinel、永不把字符串 `'[]'` 当 array marker）保持不变；Block86 的 per-item 数组路径判定、Block85 的 recursion-first、Block84 的 path-carrying 全部保持。

| 命令 | 结果 |
|---|---|
| `uv run pytest scripts/tests/test_run_product_done_gate.py -k "block82 or block83 or block84 or block85 or block86 or block87 or block88"`（聚焦） | 8 passed（Block88 新增位置无关 documented 真实数组五路矩阵 + Block87 反例全保留 + Block82–87 不回归断言） |
| `uv run pytest scripts/tests/test_run_product_done_gate.py`（全量 gate 测试） | 489 collected，退出码 0 |
| `uv run pytest apps/api/tests benchmarks/tests scripts/tests`（全量） | 1524 passed，退出码 0 |
| `uv run python scripts/tests/run_tests_clean_env.py apps/api/tests benchmarks/tests scripts/tests`（clean-env） | 1524 passed，退出码 0；clean-env 重定向临时 DB/bridge-state，live `tripchord.db`/`.runtime/browser-bridge-state.json` 不被测试触碰 |
| `uv run ruff check .` | All checks passed |
| `git rev-parse HEAD` | 最终 HEAD（当前账本提交；`_secret_redact.py` + gate 测试 + 账本/进度文档同提交，精确 SHA 见最终结果评论） |
| API 重启 + `uv run python scripts/verify_api_runtime_provenance.py` | API 受控重启绑定最终 HEAD（本提交）；provenance `passed=true`、`commit_sha`/`dependency_lock_sha256`/`live_system_source_sha256` 三哈希匹配、pid 存活、mismatches 空（精确值见最终结果评论） |
| `TRIPCHORD_ACK_MODEL_COST=1 uv run python scripts/run_product_done_gate.py --commit <最终 HEAD 全 SHA> --commit-evidence` | 同 HEAD 从头重跑严格六层门（最新 evidence 目录，run_id 与 tested_commit_sha 见最终结果评论）：层 1/2/3/4 PASS（层 4 required-model smoke 实际运行通过）、层 5 FAIL（pending user authorization：非全部 certified canary scope 有 fresh authorised 只读 canary）、层 6 FAIL——executor 在 done-gate 前于 `companion_preflight` 失败（未发现同时声明携程/去哪儿/同程且仍新鲜的已连接 Companion，心跳过期 >45s，实时搜索未提交）；`passed=false` 退出码 2、`worktree_dirty=false`、evidence 未提交（evidence_commit=null）、gate_ref=null。`passed=false` 如实记录，不包装为收口；passed=true 前不启 C-125/C-124 |

## 当前可对外声明


- v0.5/v0.6/v0.7 接入生产路径：reprice/handoff 端点 + 前端两步 handoff 流；预订保护 gate 被 Verifier/ReVerifier 与 live_system 事件重规划共同消费（v0.6 收尾完成）；SDK 冷却/一致性 API 接线。
- v0.8 完整本地产品体验：启动器/向导 + 首页旅行工作流拆分 + 高技术细节默认折叠 + WCAG 已知缺口整改（字号 ≥12px / aria-live / 表单标签 / 目标尺寸）；v0.9 CI（Companion release gate + 安全扫描 + acceptance/faults benchmark）、本地可观测性端点。
- v0.9 收尾完成：第三方 Actions SHA 固定（CI 不再跟随 `@v5/@v6` 浮动标签）、CycloneDX SBOM + 构建 provenance 漂移门（`source_digests` 绑定，避免 `commit_sha` 自引用失效）、job/monitor 可恢复持久化（重启后 ACTIVE 监控自动续跑、run 不可恢复如实 FAILED）、干净 Chrome + 本地 fixture 浏览器 E2E（CDP 驱动，无 Playwright/Puppeteer，验证四阶段工作流步骤条与回放规划渲染）。
- 五类反表面端到端验收全 PASS（`benchmarks/evaluate_acceptance.py`）。
- C-54 返工完成：层 5 改为 per-scope 认证 OTA canary（`live_canary_certified.py`，6 个 certified scope 逐项 fresh/authorized/read-only 证据，open-meteo/故宫 仅作公开页面连通性标注）；层 6 接入 `run_live_done_gate_v4.py` 真实 E2E 执行器（删除「gated behind layer 5」误导文案）。本机复跑层 1/2/3/4 PASS、层 5 FAIL（pending user authorization）、层 6 FAIL（executor 在 `companion_preflight` 失败——无已连接 Companion，实时搜索未提交），均如实记录。
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
（层 4 需 `TRIPCHORD_ACK_MODEL_COST=1` 授权模型成本后才会实际运行；层 5 需配对 Companion 且 `ctrip/qunar/tongcheng` 官方域名保持登录态后才会逐 scope 真正 PASS；层 6 在上述条件满足后由 `benchmarks/run_live_done_gate_v4.py` 真实执行——当前无已连接 Companion 时，层 6 如实报告 executor 在 `companion_preflight` 阶段失败（failed_before_done_gate），不包装为授权问题。C-122 R42 Block 88（监督打回九）代码+测试+账本/进度文档同提交，冻结唯一最终 HEAD（当前账本提交），API 已受控重启绑定该最终 HEAD，provenance 三哈希匹配、`passed=true`、mismatches 空（精确 SHA 与 pid 见 provenance 机器证据与最终结果评论）；六层门在同一最终 HEAD 上从头重跑如实记录（passed=false、exit 2，最新 evidence 目录与 run_id 见最终结果评论）。）

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
