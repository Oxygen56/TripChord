# v1.0 Done-Gate 第十五轮（C-113）阶段评审：先修层2/3根因与证据合同/落盘/原子性

> 结论：**修复已提交并验证，受控重启完成（C-114 更正：该重启绑定的是 `e862a98`，并非最终 HEAD），层 5/6 仍待用户配对（推进中）** —— 本轮按监督续跑指令先修根因、不等待配对；四项缺陷（层 2/3 宿主 bridge-state 泄漏、证据合同缺层 5/6 原始输入、证据落盘权限/链接/秘密扫描、两阶段 commit 非原子）全部修复并有真实临时仓库反例测试；API 已 `launchctl kickstart -k` 受控重启。
>
> **C-114 更正（2026-08-10）**：原结论中「API 已绑定新 HEAD」与「`passed=true` 仅受制于用户侧唯一动作（浏览器配对）」均不成立——独立代码审查在重启后又提出 8 项硬性要求（R1–R8：层 6 真实 schema 合同、层 5 真 JSON 判定、staging 独占/run_id/层3 exit-2、秘密扫描 fail-closed 与多类模型 key、compact 证据独立复核内容、symlink/hardlink/0600 落盘安全、只读 live-state lease preflight、文档结论更正与最终 HEAD 重启）。C-113 的 `e862a98` 重启早于这些修复，不能声称「绑定最终 HEAD」；最终代码与文档全部提交后需在真实最终 HEAD 再次受控重启并生成机器证据。

## 1. 层 2/3 根因：宿主 bridge-state 泄漏进测试 — 通过

- **根因**：Done-Gate 运行环境设置了 `TRIPCHORD_BROWSER_BRIDGE_STATE_PATH=<live>`，`BrowserTaskBridge()` 默认从 env 恢复宿主在途/排队/已认领 lease，使取消与配对反例测试依赖宿主残留状态——同一代码裸 shell 通过、gate 环境下失败。
- **修复**：`apps/api/tests/conftest.py` 新增 session 级 autouse fixture，在任意 module 级 fixture 构造 bridge 之前清空该 env，teardown 恢复；依赖文件存储的持久化测试显式传 `state_store`，不受影响。
- **反例**：取消场景重复 3 次确定性收敛（11 CANCELLED / 6 claimed / 晚到认领为空）；3 并发 run 共享一个 bridge 高负载取消零残留（33 任务全 CANCELLED）；取消后重启经文件 store 队列为空。
- **证据**：`TRIPCHORD_BROWSER_BRIDGE_STATE_PATH=<live> uv run pytest scripts/tests/test_run_product_done_gate.py apps/api/tests/test_browser_cancellation.py -q` 全部通过。

## 2. 证据合同降门：层 5/6 原始 evidence 强制清单 — 通过

- **修复**：`_REQUIRED_EVIDENCE_INPUTS` 固定 5 项（`product-acceptance.json`、`browser-e2e.json`、`browser-e2e-screenshot.png`、`live-canary-certified.json`、`live-done-gate-v4.json`），`--commit-evidence` 前缺任何一项立即 exit 2，不得因 gitignore 静默省略；提交后硬校验清单、每文件 SHA256、父链（E^=S、P^=E）与字段完整（manifest 必需键、file entry 字段、layer_verdicts 的 5/6 键）。
- **反例**：缺层 6 原始 evidence → exit 2 且无任何提交、HEAD 不动；缺层 5 原始 evidence → exit 2；字段不完整 manifest → 契约校验 raise；全量输入 → 通过。

## 3. 证据落盘安全 — 通过

- **修复**：staging 目录创建即 0700，层写完后整树再收紧（目录 0700、文件 0600）；读取/复制前对每个证据文件做安全校验——拒绝符号链接、硬链接（nlink>1）、非当前用户文件。
- **秘密扫描多类化**：不再只看单 token 字节——覆盖模型 API key（`MODEL_API_KEY`/`TRIPCHORD_MODEL_API_KEY` env 值）、bridge token、Authorization/Cookie 值、数字账号标识（account/user/member/order id ≥6 位）、手机号、完整 tracking URL（非 `[REDACTED]` 的 query 值）；`[REDACTED]` 与良性 query 通过不误报。
- **反例**：0700/0600 实测；symlink、hardlink、foreign-uid 各自拒绝；tracking URL、Authorization、账号 id、模型 key 泄漏各自 abort；脱敏证据通过。

## 4. 两阶段 evidence commit 原子性 — 通过

- **修复**：两阶段 commit 不再 `git commit` 移动分支两次——E 与指针提交 P 均用 `git commit-tree` 从当前 index tree 物化（HEAD 不动），分支只在最后通过**一次** `git update-ref HEAD <P> <S>` 比较-交换前进。任一阶段/add/write-tree/commit-tree/update-ref 失败：最终 HEAD 停在 S、无中间 E、index/worktree 干净、报告 `evidence_commit` 清空、`passed=true` 不落盘。
- **反例**（真实临时仓库）：phase-1 add 失败、phase-1 commit-tree 失败、phase-2 commit-tree 失败、update-ref CAS 失败——分支均在 S、无可达中间提交、porcelain 为空；成功对照组断言 S→E→P 父链、`evidence_commit` 记录、树干净。

## 5. 受控重启 — 工程侧完成，但绑定的是 C-113 中间 HEAD（C-114 更正）

- API 由 launchd（`com.tripchord.live-api`）管理，plist 为加固版安全启动器（从 0600 文件读 token/key，无内嵌秘密）。
- `launchctl kickstart -k gui/<uid>/com.tripchord.live-api` 受控重启；当时 `/api/v1/agents/runtime` provenance 三哈希（`commit_sha=e862a98…`、`dependency_lock_sha256`、`live_system_source_sha256`）与 C-113 本地树匹配。
- **C-114 更正**：`e862a98` 是 C-113 的原子提交修复，其后独立审查又提出 R1–R8 八项硬性要求（见顶部结论），因此该重启**不构成**「绑定最终 HEAD」。最终代码与文档（含 R1–R8 全部修复）提交后，须在真实最终 HEAD 再次 `launchctl kickstart -k` 受控重启并核验 provenance 三哈希，才可声称 API 已绑定最终 HEAD。

## 当前仍不能声称

- 任何 v1.0 Done-Gate 通过、双平台住宿精确报价、完整 OTA 闭环。
- 层 5/6 尚未真正执行（0 companions）：先 canary 再从头跑六层门，待用户配对 Companion 后推进；随后按指令走 Planner–Verifier–Repair–ReVerifier 与事件重规划闭环、更新账本、交不同 Agent 代码审查、再交监察官终验。
