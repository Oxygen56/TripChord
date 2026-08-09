# v1.0 Done-Gate 第十六轮（C-114）阶段评审：独立代码审查 R1–R8 修复 + 最终 HEAD 受控重启 + 六层门重跑

> 结论：**R1–R8 八项硬性要求全部修复并有真实临时仓库反例测试；最终代码与文档已全部提交（HEAD=`74cd75c`）；API 已在真实最终 HEAD 受控重启并硬校验 provenance 三哈希匹配；全量 `apps/api/tests` 852 项通过、gate 单测 121 项通过。六层门在本轮真实重跑：层 1/2/3 PASS、层 4 skip（模型成本未授权）、层 5/6 FAIL（pending user authorization）。`passed=false` 如实，未收口 C-2；`passed=true` 前唯一剩余外部动作是用户侧浏览器配对 Companion（ctrip/qunar/tongcheng 登录态）并授权 `TRIPCHORD_ACK_MODEL_COST=1`。**
>
> **C-113 结论更正**：原「API 已绑定最终 HEAD」与「`passed=true` 仅受制于用户侧唯一动作」均不成立——C-113 的 `e862a98` 重启早于独立审查提出的 R1–R8；本轮已在真实最终 HEAD 重新受控重启并生成机器证据（见 §5）。

## 0. 本轮依据

- 监督退回指令（2026-08-10 06:31）：不接受 C-113 以「唯一卡点是用户配对」停住；先修全部工程缺口。
- 独立代码审查（2026-08-10 06:35）：`5180492..66b338c` 存在 8 项硬缺陷（R1–R8），不得通过调整测试伪造 schema/接受 skip/空 scope/旧文件/仅 hash 布尔摘要来变绿。
- 红线：全程只读、禁止污染 live bridge-state/tripchord.db、passed 如实、禁止在 `/Users/oxygen/multica_workspaces` 下实施或生成证据。

## 1. R1–R8 修复与反例

### R1 层 6 真实 schema 合同 — 通过
- `layer6_full_e2e` 校验真实 `run_live_done_gate_v4` 完成产物：`done_gate.passed` + `done_gate.checks` 15 项逐项（`_done_gate_mismatches`），缺字段/错层级/任何一项未过/15 项不齐均 fail-closed；不再信任进程 exit 0 或伪造的顶层 `passed`。
- 反例测试：`test_done_gate_mismatches_*` 系列覆盖缺 checks、空 checks、单检查未过、非真实 15 项名、外层 passed 与 done_gate.passed 不一致。

### R2 层 5 真 JSON 判定 — 通过
- `layer5_real_canary` 以 certified canary JSON 驱动层判定：要求 `passed=true`、6 个 certified scope 完整、逐项 `fresh/authorized/read_only/passed`；空 scopes、缺 scope、stale、pending 均失败。
- 反例测试：`test_layer5_fails_on_missing_certified_scope`、`test_layer5_fails_on_stale_scope`、`test_layer5_fails_when_top_level_passed_false`、`test_layer5_passes_only_complete_certified_set`。

### R3 staging 独占 + run_id + 层 3 exit-2 — 通过
- 每次运行生成唯一 `run_id`（12 位 hex），嵌入 staging 目录名与报告；`_reject_target_conflict` 拒绝已存在的非空 staging 目录；报告/清单携带 `tested_commit_sha`、`generated_at`、`schema_version`、`run_id`。
- 层 3 浏览器 E2E `code4==0` 才通过；exit 2（skip）不算通过。
- 反例测试：`test_main_exits_2_when_staging_is_non_empty_dir`、`test_main_accepts_existing_empty_staging_dir`、`test_new_staging_dir_embeds_unique_run_id`、`test_run_gate_report_carries_run_id`、`test_layer3_browser_e2e_exit2_is_not_a_pass`。

### R4 秘密扫描 fail-closed + 全模型 key — 通过
- 扫描错误（OSError）改为 fail-closed（`GateStateChangedError`），不再 continue；覆盖 `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`/`MODEL_API_KEY`/`TRIPCHORD_MODEL_API_KEY` 及 AMAP/Booking/Amadeus/DeepSeek/Google/Gemini/Azure 等全部模型 key 候选；错误只报类别+文件，绝不回显匹配片段。
- 反例测试：`test_secret_scan_fails_closed_on_unreadable_file`（mode-000）、`test_secret_scan_covers_all_model_api_key_envs`。

### R5 compact 证据独立复核内容 — 通过
- 层 5 compact v2：`coverage`（expected/observed/passed scope 计数、missing）+ 每 scope `provider`/`evidence`（companion/adapter/contract/runtime id、options、searched_at、providers、authorized scope keys、样本、source_url_count，无原始 URL）。
- 层 6 compact v2：完整 `done_gate`（passed + check_count + checks 每项 name/passed/summary/evidence_refs）+ 查询/候选/报价/provider 绑定 + 覆盖阈值 + P-V-R + 预算 + 事件注入/重规划 + repo/runtime/Companion identity。
- 提交后从证据提交 E 中读取 manifest/compact blob 再核验（sha256 + 内容结构），不只校验内存对象；files 精确覆盖固定清单。
- 反例测试：`test_compact_canary_carries_coverage_and_bindings`、`test_compact_live_e2e_carries_15_checks`、`test_verify_evidence_contract_rejects_blank_compact_content`。

### R6 symlink/hardlink/0600 落盘安全 — 通过
- `_lstat_safe_check`：staging 根与任意子目录先用 `lstat` 拒绝 symlink、hardlink（nlink>1）、非当前用户文件；`chmod` 不跟随外部目标。`--output` 即使在 staging 外也经临时文件 0600 + 原子 rename。
- 反例测试：`test_harden_staging_rejects_symlink_subdir`、`test_harden_staging_rejects_symlink_root`、`test_dump_output_atomic_0600_outside_staging`。

### R7 只读 live-state lease preflight — 通过
- `_live_state_lease_preflight`：以 sqlite `mode=ro` 严格只读打开 live-state DB，检测 `jobs` 表中 `queued/running` 且 lease 未过期的残留 lease；残留则层 6 在提交新 E2E 前拒绝运行。preflight 只能读、不能清/续 lease，因此不会掩盖其要检测的残留。
- 快照隔离测试用临时 SQLite（绝不动 live `tripchord.db`）：残留 queued/claimed 检测、无 lease 的 queued 检测、干净库通过、过期 lease 忽略、缺失库 fail-closed、字节级只读证明、层 6 集成（残留阻断 / 干净放行）。
- 测试：`test_live_state_lease_preflight_*`、`test_layer6_fails_when_residual_lease_present`、`test_layer6_lease_preflight_passes_on_clean_live_state`。

### R8 文档更正 + 最终 HEAD 重启 — 完成
- `product-v1.0-round15-c113.md` 与 `productization-progress.md` 中「API 已绑定最终 HEAD / 唯一只差用户配对」结论已更正（见 §2）。
- 最终代码与文档全部提交后，API 已在真实最终 HEAD 受控重启并硬校验 provenance（见 §5）。

## 2. 文档结论更正

- `docs/phase-reviews/product-v1.0-round15-c113.md`：顶部结论与 §5 已加 **C-114 更正**——C-113 重启绑定的是 `e862a98` 中间 HEAD，审查又提出 R1–R8，不能声称「绑定最终 HEAD」；`passed=true` 并非「仅受制于用户侧唯一动作」。
- `docs/productization-progress.md`：当前状态、版本表 v1.0 行已改为 C-114 状态与更正说明。

## 3. 提交

- `8fe8c78` fix(v1.0 done-gate): C-114 review R1-R7 — real-schema contract, exclusive staging, fail-closed secrets, compact evidence, lease preflight
- `74cd75c` docs(v1.0 done-gate): C-114 R8 — correct 'only user pairing left / API bound to final HEAD' conclusions

## 4. 全量回归（隔离、可复现、不污染 live 状态）

- **全量 `apps/api/tests`**：`TRIPCHORD_DATABASE_URL` 指向临时 SQLite，852 项全部通过，退出码 0，未触碰 live `tripchord.db`/bridge-state。
- **gate 单测** `scripts/tests/test_run_product_done_gate.py`：121 项通过。
- 负对照：live-state lease 预检测试全部使用临时库快照，未污染真实 DB。

## 5. 最终 HEAD 受控重启 + provenance 硬校验（机器证据）

- 最终 HEAD：`74cd75c681277de034a9a4403ee725d3690a9904`（branch `productization/v1.0`，工作树干净）。
- `launchctl kickstart -k gui/501/com.tripchord.live-api` 受控重启；新 PID=`93986`。
- `/api/v1/agents/runtime` 用 `runtime_provenance.validate_runtime_provenance` 硬校验：
  - `commit_sha=74cd75c681277de034a9a4403ee725d3690a9904` ✅
  - `dependency_lock_sha256=2feff8c1…` ✅（与本地 `uv.lock` 一致）
  - `live_system_source_sha256=fbacc8f6…` ✅（与本地 `live_system.py` 一致）
  - `pid=93986` 存活、`python_version=3.12.13`、`started_at=2026-08-09T23:36:43Z` ✅
  - 机器证据输出：`runtime_provenance.validate_runtime_provenance` 返回空 mismatch 列表。

## 6. 六层门重跑（run_id=`e5ba5325692d`，tested_commit_sha=`74cd75c`，generated_at=2026-08-09T23:38:42Z）

| 层 | 判定 | 说明 |
|---|---|---|
| 1_reproducibility | **PASS** | alembic upgrade/check、web build、api import、secret redaction、sbom drift 全过 |
| 2_replay | **PASS** | verifier/planning/repair/events benchmarks + 6 反表面 acceptance 全过 |
| 3_clean_chrome_fixtures | **PASS** | handoff URL policy、bridge permission、reprice/booking/wiring、clean headless Chrome E2E 真实渲染全过 |
| 4_model_smoke | **skip** | 模型 key 在环境但 `TRIPCHORD_ACK_MODEL_COST` 未授权——skip 不失败也不通过 |
| 5_real_canary | **FAIL** | 5 个浏览器 scope 无 Companion 心跳（0 companions）；`icom:transfer` 真实只读公共 API 返回 7 个 transfer 选项 PASS；public_page_connectivity PASS；companion_status 端点可达 PASS |
| 6_full_e2e | **FAIL** | pending user authorization：`TRIPCHORD_ACK_MODEL_COST=1` 未授权，不运行 live E2E |

- **15 项 done_gate checks**（层 6 内层门）：因层 6 卡在用户授权闸，本轮**未运行**——不虚构通过，如实记「pending user authorization」。
- 报告：`.runtime/done-gate-evidence/gate-20260809T233822Z-e5ba5325692d/product-v1-done-gate.json`，`passed=false`、`evidence_commit=null`，退出码 2，未生成任何证据提交。
- 门后工作树干净（`git status --porcelain` 为空），无 live 状态写入。

## 7. 当前仍不能声称 / 下一步

- 不声称：v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环、层 6 15 项 checks 通过。
- `passed=true` 前的剩余用户侧最小动作：**浏览器配对 Companion**（连接并保持 ctrip/qunar/tongcheng 官方域名登录态）+ **`TRIPCHORD_ACK_MODEL_COST=1`**（授权有界模型成本）。
- 配对后：先 canary，再从头跑六层门；`passed=true` 前继续修真实双平台精确报价、P-V-R、事件注入与动态重规划；随后交不同 Agent 代码审查、监察官独立终验。
