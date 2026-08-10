# v1.0 Done-Gate 第十七轮（C-118）阶段评审：八项硬缺口原地修复 + 最终 HEAD 受控重启 + Companion 自动重配 + 六层门从头重跑

> 结论：**C-116 监督复核提出的八项硬缺口全部原地修复，并有真实临时仓库/端到端反例测试（gate 单测 138 项通过）；代码与文档全部提交后（最终代码+文档提交 `048ba57`，本评审为最后文档提交），API 已在真实最终 HEAD `048ba57` 受控重启并硬校验 provenance 三哈希匹配（机器证据见 §5）；Companion 自动重配结果如实记录（§6）；模型成本已授权（`TRIPCHORD_ACK_MODEL_COST=1`），六层门从头重跑：层 1/2/3/4 PASS、层 5/6 FAIL（pending user authorization，5 个浏览器 scope 无 Companion 心跳）。`passed=false` 退出码 2 如实，未生成任何证据提交（evidence_commit=None）。`passed=true` 前的剩余用户侧最小动作仍是浏览器配对 Companion 并保持官方域名登录态。**
>
> **C-118 更正**：C-116 打回 C-114 后，本轮不再提前宣称通过；真实门未过、E 未生成前不宣称 compact 已提交。账本与交付声明已按八项缺口更正（§2）。

## 0. 本轮依据

- 监督退回指令（2026-08-10）：不接受 C-114 以「唯一卡点是用户配对」停住；八项硬缺口必须有真实临时仓库/端到端反例测试并原地修复。
- 红线：全程只读、禁止污染 live bridge-state/tripchord.db、passed 如实、禁止在 `/Users/oxygen/multica_workspaces` 下实施或生成证据、禁止降低阈值/伪造平台证据/删除测试/放宽断言/接受 skip/空 scope/旧 evidence/把 hash 或布尔摘要当独立证据。

## 1. 八项硬缺口修复与反例（gate 单测 138 项通过）

### ① update-ref CAS 后 report dump 失败路径 — 通过
- 原缺陷：CAS 成功后 main 再执行可能失败的 report dump，失败会以「passed=true 指针已安装」的状态失败退出，制造歧义。
- 修复：post-CAS re-dump 包在 try/except 中，失败不再翻转退出码；CAS 成功后运行状态不可变。
- 反例测试：`test_main_post_cas_redump_failure_does_not_flip_exit`——第 4 次 `_dump` 强制失败，断言 rc==0 且 `[REDACTED]` 落盘、token 不在盘上。
- 原泄漏测试改写：`test_commit_evidence_catches_last_step_report_leak` → `test_commit_evidence_neutralizes_last_step_report_leak`（rc==0 + 落盘脱敏）。

### ② 层 5 canary 退出码 + certified JSON 全绿合同 — 通过
- 原缺陷：层 5 只信子进程退出码或只信 JSON 顶层 `passed`，非零退出 + 伪全绿 JSON 可能漏过。
- 修复：`layer5_real_canary` 同时要求 canary 子进程退出码 0、certified JSON `passed=true`、精确六 scope（`_CERTIFIED_OTA_SCOPES`）且逐项 `fresh/authorized/read_only/passed`；非零退出+伪全绿 JSON 必败；非认证额外 scope 必败。
- 反例测试：`test_layer5_fails_when_canary_exits_nonzero_despite_green_json`、`test_layer5_rejects_extra_scopes`。

### ③ 所有退出路径秘密扫描 + 源头脱敏 — 通过
- 原缺陷：仅部分路径扫密，失败门/不提交运行可能把子进程 stdout、Cookie、Authorization、API key、账号或完整 tracking URL 写盘或打印，错误可能回显原文。
- 修复：`_run` 源头脱敏（`_redact_output`，`[-2000:]` 截断前先脱敏）、`_dump` 落盘就地脱敏（`_redact_report`）、main 所有退出路径在 report/manifest/compact 写完后 `_secret_scan_paths` 扫描（泄漏即 unlink）、`_safe_print_report` 吞 BrokenPipe；错误经 `_redact_output(str(exc))` 不回显原文。
- 反例测试：`test_main_failed_gate_report_is_redacted`。

### ④ residual lease 预检读实际 bridge-state JSON — 通过
- 原缺陷：层 6 lease 预检只查 `tripchord.db` planning jobs，漏掉 Bridge 侧 `TRIPCHORD_BROWSER_BRIDGE_STATE_PATH` 对应 JSON 的 queued/claimed/重排状态。
- 修复：`_bridge_state_lease_preflight` 读取 `_resolve_bridge_state_path` 解析出的实际 bridge-state JSON，检测 `_BRIDGE_TASK_RESIDUAL_STATES`={queued,claimed} 与 `_BRIDGE_CONTROL_RESIDUAL_STATES`={queued,draining,dispatched,accepted}；symlink/missing fail-closed；`layer6_full_e2e` 在 `_live_state_lease_preflight` 之后追加此预检。
- 反例测试：`test_bridge_state_lease_preflight_detects_queued_work`、`accepts_terminal_states`、`fails_closed_on_missing_file`、`test_resolve_bridge_state_path_honors_explicit_and_env`、`test_layer6_fails_when_bridge_state_holds_residual_work`。

### ⑤ evidence commit 全程临时 index — 通过
- 原缺陷：并发 HEAD/CAS 失败时可能 reset 分支或真实 index 到旧 S，丢失已测试提交的指针。
- 修复：两阶段提交全程使用临时 `GIT_INDEX_FILE`（`commit-tree` E 父=S → `commit-tree` P 父=E → `git update-ref HEAD <P> <S>` CAS 最后一步）；任何阶段失败 `read-tree <当前 HEAD>` 回滚，绝不回退到旧 S。
- 反例测试：`test_commit_evidence_*` 系列覆盖 phase-1/2 add/commit-tree/update-ref CAS 失败原子性、成功父链 S→E→P。

### ⑥ staging 根 lstat 拒 symlink + 独占创建 + 原子 0600 — 通过
- 原缺陷：staging 根可能跟随 symlink，已有空目录被复用，输出权限不统一。
- 修复：`_reject_target_conflict` 基于 lstat（symlink 即拒）；kind=="dir" 时任何已有目录（含空目录）均拒绝；`staging_dir.mkdir(parents=True, mode=0o700)` 无 `exist_ok`；`_write_atomic` uuid 临时文件 → chmod 0600 → os.replace。
- 反例测试：`test_main_rejects_staging_symlink`。

### ⑦ compact 合同从 E 的 blob 回读硬校验 — 通过
- 原缺陷：compact 只在内存校验，层 5 精确六 scope、层 6 精确十五项等合同可能被提交侧绕过。
- 修复：`_verify_layer5_compact_contract` 从证据提交 E 的 blob 回读：精确六 scope、每项 fresh/authorized/read_only/passed；`_verify_layer6_compact_contract` 回读：精确十五项且全部通过，并保留结构化报价绑定、覆盖阈值、P-V-R-ReV、预算、事件注入/重规划与 repo/runtime/Companion identity。
- 反例测试：`test_verify_layer5_compact_contract_accepts_full_set`/`rejects_incomplete_scope_set`/`rejects_non_certified_scope`、`test_verify_layer6_compact_contract_accepts_full_passing_set`/`rejects_non_passing_check`/`rejects_missing_identity`。

### ⑧ 账本与交付声明更正 — 完成
- `docs/claim-ledger.md` 增补 C-118 两行（八项硬缺口修复与 gate 单测 138 项、隔离回归 1016 项）；`docs/productization-progress.md` 当前状态/版本表/第十七轮验证结果表改为 C-118 状态并明确「真实门未过、E 未生成前不宣称 compact 已提交」；本评审（§2/§5/§6/§7）为交付声明依据。

## 2. 账本与交付声明更正

- 不提前宣称通过：C-116 打回后，本轮以「真实门未过、E 未生成前不宣称 compact 已提交」为准，任何 fix 的通过声明只限定在对应单测/回归证据内。
- 隔离回归是工程证据，不是六层门通过：层 5/6 真实运行仍取决于 Companion 配对与授权。

## 3. 提交

- `a050ad2` fix(v1.0 done-gate): C-118 eight hard gaps — atomic post-CAS, canary exit+scope contract, redact-at-source, bridge-state lease preflight, temp-index commit, exclusive staging, compact blob contract
- `048ba57` docs(v1.0 done-gate): C-118 Gap 8 — correct ledger + delivery statement before the final-HEAD restart
- docs(v1.0 done-gate): C-118 round-17 phase review + gate-run result rows（本评审所在提交，最终文档提交）

> 注：API 受控重启与六层门运行均绑定 `048ba57`（代码+文档全部提交后的最终 HEAD）；本评审为纯文档提交，不改变运行代码，不构成新的运行绑定。

## 4. 全量回归（隔离、可复现、不污染 live 状态）

- **gate 单测** `scripts/tests/test_run_product_done_gate.py`：138 项通过，含八项硬缺口反例（见 §1）。
- **全量隔离回归** `uv run pytest apps/api/tests/ scripts/tests/ --ignore scripts/tests/test_browser_e2e.py`：1016 项通过，退出码 0；未污染 live `tripchord.db`/bridge-state（conftest session 级清 bridge-state env、临时 SQLite 快照）。

## 5. 最终 HEAD 受控重启 + provenance 硬校验（机器证据）

- 执行顺序遵循 Gap 8：**最终代码与文档全部提交后**（HEAD=`048ba57`），才在真实最终 HEAD 受控重启。
- `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` 受控重启成功。
- `/api/v1/agents/runtime` 实测（重启后、本轮评审时）：
  - `commit_sha`=`048ba57d17e65bca2529b9e37a58dc035ccecd11`，与 `git rev-parse HEAD` 完全一致；
  - `dependency_lock_sha256`=`2feff8c1917c005a0300438660f14cad0f8a9c20fc33728bab47258b0d134e8f`，与本地 `uv.lock` 的 `shasum -a 256` 一致；
  - `live_system_source_sha256`=`fbacc8f63d5ebb7846c94ce1d760b7b68429ae0cd11eb7be71323a800679f5b8`，与本地 `apps/api/src/tripchord/agents/live_system.py` 的 `shasum -a 256` 一致；
  - `pid`=35130 存活、`python_version`=3.12.13、`started_at`=2026-08-10T00:27:28Z。
- 三哈希匹配、pid 存活，机器证据为权威绑定。

## 6. Companion 自动重配结果（如实）

- `/api/v1/agents/runtime`：`browser_companion_auto_reload_enabled=true`、`browser_companion_supervisor_running=true`、`browser_companion_supervisor_outcome=waiting_for_control_capable_companion`、`browser_companion_supervisor_attempt_count=0`、`browser_companion_control_enabled=false`。
- `/browser-bridge/v1/companions/status`：0 companions、`status=disconnected`、`stale_after_seconds=45`。
- `.runtime/browser-bridge-state.json`：`tasks=[]`、`reload_requests=[]`（无残留，层 6 lease 预检放行层）。
- 结果：**未配对任何 control-capable Companion**；自动重载 supervisor 在运行并等待可控制 Companion。这是用户侧动作，本轮不伪造配对。

## 7. 六层门重跑（run_id=`ff7492050865`，tested_commit_sha=`048ba57`，generated_at=2026-08-10T00:32:00Z，模型成本已授权）

| 层 | 判定 | 说明 |
|---|---|---|
| 1_reproducibility | **PASS** | migration upgrade/check、web build、API import、secret redaction、sbom drift 全过 |
| 2_replay | **PASS** | verifier/planning/repair/events benchmarks + 五类反表面 acceptance 全过 |
| 3_clean_chrome_fixtures | **PASS** | handoff URL policy、bridge permission、reprice/booking wiring、clean headless Chrome E2E 真实渲染全过 |
| 4_model_smoke | **PASS** | `TRIPCHORD_ACK_MODEL_COST=1` 已授权，required-model smoke 真实运行通过 |
| 5_real_canary | **FAIL** | 5 个浏览器 scope 无 Companion 心跳（0 companions，pending user authorization）；`icom:transfer` 真实只读公共 API 返回 7 个 transfer 选项 PASS；companion_status 端点可达（disconnected） |
| 6_full_e2e | **FAIL** | 层 5 前置未满足，runner `run_status=failed_before_done_gate`、无 done_gate report，evidence cross-check 失败；pending user authorization |

- **15 项 done_gate checks**（层 6 内层门）：因层 6 卡在用户授权闸，本轮**未运行**——不虚构通过，如实记「pending user authorization」。
- 报告：`.runtime/done-gate-evidence/gate-20260810T003137Z-ff7492050865/product-v1-done-gate.json`，`passed=false`、`evidence_commit=None`、退出码 2、`worktree_dirty=false`，未生成任何证据提交。
- 门后工作树干净（`git status --porcelain` 为空）、HEAD=`048ba57` 不变；`.runtime/done-gate-evidence/` 为 gitignored 运行时目录，非提交证据。

## 8. 当前仍不能声称 / 下一步

- 不声称：v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环、层 6 15 项 checks 通过、compact 已提交。
- `passed=true` 前的剩余用户侧最小动作：**浏览器配对 Companion**（连接并保持 ctrip/qunar/tongcheng 官方域名登录态）后重跑；模型成本授权已就绪（`TRIPCHORD_ACK_MODEL_COST=1`）。
- 配对后：先 canary，再从头跑六层门；`passed=true` 前继续修真实双平台精确报价、P-V-R、事件注入与动态重规划；随后交不同 Agent 代码审查、监察官独立终验。
