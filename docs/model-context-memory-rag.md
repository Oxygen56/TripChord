# 模型接线、上下文工程、记忆与 RAG

## 现在的直接答案

- 仓库安全默认：`MODEL_PROVIDER=none`、`MODEL_AGENTS_REQUIRED=false`，不会自动产生付费调用；
- 推荐的 required-model 配置：OpenAI-compatible 网关 + DeepSeek `deepseek-v4-flash`；
- 历史模型记录：2026-08-03 JSON 记载了一次结构化输出和一次有界 tool loop，但原始一次性
  调用早于可复现 runner，不能仅凭该文件做字节级独立复现；
- 当前复现入口：`scripts/run_model_runtime_smoke.py` 强制 `--ack-live-cost`、只从环境读取 key，
  输出 request/response/schema/code hash、trace、usage 与 latency；2026-08-04 已用
  `deepseek-v4-flash` 真实执行 3 次有界请求，结构化 JSON 与工具循环均通过；
- 同日 required-model Chrome canary 真实进入 10 个模型 Agent 阶段、20 次逻辑请求，但因部分
  复杂结构化阶段失败及严格平台覆盖不完整而由最终发布门输出 `HUMAN_BLOCK`；该负结果证明
  fail-closed，不是完整 OTA 模型 E2E 成功证据；
- 后续三日期 Round 17 记录 job-bound 47/47 成功模型调用和三个 pair checkpoint，但业务 runner
  为 `done_gate_failed`；其中 Evidence Arbiter policy 冲突修复后的同日期 focused 运行又以
  23/23 成功调用完成。最终仍因住宿精确报价 provider 只有 1/2 而 `HUMAN_BLOCK`；
- 尚无证据：DeepSeek 驱动整套 Chrome OTA Done-Gate 成功、长期生产稳定性或比其他模型更优；
- 也支持 Anthropic Messages，但当前没有同等级 live smoke 证据。

模型接通后，需求理解、日期查询、来源调度、证据仲裁、候选策展、Risk Critic、Repair
Strategist、ReCritic、Event Diagnoser、主控建议、解释与记忆候选会走受限模型 Agent；
报价解析、金额、稳定身份、新鲜度、权限、Verifier/ReVerifier、Repair Executor 和最终
Safety Gate 始终保持确定性。

TripChord 不依赖 Codex 或 ChatGPT runtime。真实 OTA 运行也不可能只靠 LLM API Key：还要
本地 browser bridge、Chrome Companion、用户授予的 provider 域名权限、有效登录态、网络和
仍匹配当前网页的 DOM 合同。

## 历史审计与当前接线状态

早期审计发现，仓库当时只有模型适配器和 replay `ScriptedModelClient`，实时浏览器整包路径并未
显式构造真实 router。这个缺口现已修复：`main.py` 会根据显式 `MODEL_PROVIDER` 配置构造
primary/fast client 与 `ModelRouter`，并把同一个 router 接入 `HybridPackageRequirementAgent`、
`FlexibleLiveAgentSystem` 和 `LivePackageAgentSystem`。需求理解、Query Strategist、Search
Supervisor、证据仲裁、候选策展、Risk Critic、Repair Strategist、ReCritic、Event Diagnoser、
主控建议、解释与记忆候选因此都能在 live 路径使用真实模型。

仍需区分三个事实：

- `/api/v1/agents/plan` / `replay-plan` 仍是明确标注的 replay 演示路径，不能作为 live 模型证据；
- 默认 `MODEL_PROVIDER=none` 时 router 为 `None`，可选 Agent 会带原因降级；只有显式配置模型并在
  trace 中看到非 scripted provider/model，才能说本次 live run 实际使用了模型；
- LoRA SFT/DPO 仍只是离线训练与 adapter reload 证据，没有接入 live 推理链路；Round 17 已证明
  DeepSeek + Chrome + 多阶段 Agent 可以完成三日期控制面，但统一 OTA 业务 Done-Gate 仍未通过。

因此准确表述是“主应用已完成真实模型接线；DeepSeek V4 Flash 网关 smoke 与 Round 17/focused
真实模型路径均已运行，但被双平台住宿证据门阻塞；完整 OTA 模型 E2E 尚未通过”，而不是旧结论
“实时路径没有 LLM”，也不是
过度结论“只填 API Key 就已经完成真实 OTA 多 Agent 闭环”。

2026-08-04 canary 与当前 strict bundle 的可复核证据包括
`benchmarks/results/model-runtime-smoke-deepseek-v4-flash-2026-08-04.json`、
`benchmarks/results/live-deepseek-v4-flash-canary-2026-08-04.json`、
`benchmarks/results/live-done-gate-v4-round17-async-v13.json`。前一 live canary 记录了需求解析就绪、
Query Strategist 选择 2026-08-20 至 2026-08-26、三平台后台查询、模型调用计数、费用估算、
平台 DOM/覆盖失败以及服务恢复结果；原始大回执仅保存在私有 `.runtime` 文件中。

## 模型网关

`agents/model_gateway.py` 现在提供无特定 SDK 依赖的两类 HTTP 客户端：

1. Anthropic Messages API；
2. OpenAI-compatible `/v1/chat/completions`，可用于兼容该协议的托管或本地服务。

`ModelClientConfig` + `build_model_client` 是 provider-agnostic 工厂。网关还提供：

- 本地 JSON Schema 失败关闭校验；OpenAI-compatible 供应商同时获得原生
  `response_format=json_schema`；
- 可配置超时、有界指数重试与不可重试 4xx 快速失败；
- `ModelRouter` 保留按角色/风险路由与显式 fallback；
- 调用 trace 只保留 prompt digest，不保留 prompt 明文，并记录 token、延迟和可选
  成本估算。成本单价是运行配置，不内置可能过期的供应商价目表。

运行回执将模型参与拆成五个不可混用的口径：

- `stage_count`：纳入运行回执的 Agent 阶段数，包括未配置模型而降级的阶段；
- `model_stage_count`：实际进入过模型路由的阶段数；
- `logical_request_count`：每次 `router.complete` 计一轮，tool loop 会产生多轮；
- `http_attempt_count`：进一步计入主模型重试与 fallback 请求；
- `total_latency_seconds` / `total_estimated_cost_usd`：分别汇总模型路由墙钟时间与已有
  usage 证据的估算费用。

旧字段 `model_call_count` 仅为 API 兼容保留，现与 `logical_request_count` 同义，
不再代表 Agent 阶段数。
`ScriptedModelClient` 离线测试中的 `http_attempt_count` 只表示 client attempt，不得当作真实网络请求证据。

`Settings.model_client_config(fast=False)` 用于将环境变量转成网关配置。通用变量为
`MODEL_PROVIDER`、`MODEL_API_KEY`、`MODEL_BASE_URL`、`MODEL_NAME`、`MODEL_FAST_NAME`
以及超时/重试/单价字段。旧 Anthropic 字段保留向后兼容。

### 外部模型隐私边界

启用外部 endpoint 后，预算化用户需求、结构化证据摘要和角色获准观察的工具回执会发送给模型；
它们可能包含个人行程偏好与报价摘要。TripChord 不把浏览器 Cookie、登录凭据、Chrome profile
或 bridge pairing secret 放入模型上下文，内部 trace 只保存 prompt digest。

但外部供应商仍会收到真实输入，其日志、训练、保留和地域政策由所选 endpoint 决定。当前项目
不提供企业级 DLP、字段级脱敏、数据驻留或地域合规承诺；敏感场景应选择符合组织政策的自托管/
合规 endpoint，或保持 `MODEL_PROVIDER=none`。

## 记忆模型

`agents/memory.py` 把记忆分成：

- 短期工作记忆：必须绑定 session 与 TTL；
- 事件/情节记忆：保留已发生事件、决策和修复结果；
- 用户偏好记忆：用户明示偏好与来源；
- 可检索证据记忆：非实时证据与平台能力记录。

每条记忆都带 tenant/user/session/trip 作用域、隐私边界、来源、版本、采集时间、
TTL、置信度、可见角色、敏感标记和污染标记。存储契约还限制 payload 字节数、
嵌套深度、容器大小和单个字符串长度；疑似 prompt injection 的内容必须标记为
`tainted` 且不得进入 RAG。`MemoryStore` 是线程安全的进程内参考实现；
生产多实例需用持久层实现同一契约。

Memory Curator 只生成 pending-confirmation 候选；无论 `trip` 还是 `user` 作用域都强制
`requires_user_confirmation=true`，不会由最终 Publication Gate 自动写进 RAG。自动持久化只
保存确定性 Safety Gate 的历史决策回执。用户偏好只能通过显式确认接口写入，且提供按 record id 的撤销接口。
`development-anonymous` 没有稳定用户身份，因此不允许创建、查看或撤销 USER 长期记忆，
也不会将共享字面值 `anonymous` 写成私有用户/行程记忆。

## RAG 边界

`agents/rag.py` 只允许检索：

- 用户偏好；
- 历史决策/修复结果；
- 平台能力与数据契约；
- 非实时、仍新鲜且明确标注 `rag_eligible` 的证据。

实时价格、余票、库存与可订状态一律标为 `REALTIME + rag_eligible=false`，必须从当前工具
回执进入当前 Context Pack，不能被写成静态知识后在未来行程中复用。

这里的 RAG 是 BM25 词法检索，不是向量库或旅行知识库。`provider_capability`
不再是测试占位符：live 请求建立上下文时，会把当前确定性 provider registry 中的
只读 vertical、是否需要浏览器登录、禁止下单等事实按 tenant 幂等写入；其中不含实时价格。

## 按 Agent 预算的 Context Pack

`agents/context_budget.py` 为 Query、Planner 与 Repair 定义独立 token 预算。构建顺序是：

1. 当前用户请求（必须保留）；
2. 当前关键工具/Verifier 证据（可指定为不可裁剪）；
3. 其他当前新鲜证据；
4. 经作用域、隐私、TTL 与 RAG 边界筛选的历史记忆；
5. 从同一预算预留的空间中加入后续工具观察。

若当前请求或不可裁剪的 Verifier 拒绝理由超过整个预算，构建器失败关闭，不会静默
丢弃。当预算化 Context Pack 存在时，模型 prompt 只注入这一份表示，不会再追加一份
原始 Blackboard Context Pack。工具回执会被包装为 `untrusted_tool_data`；太大时仅给出
带摘要 hash 和原始字节数的显式截断预览，连截断元数据都放不进剩余预算时失败关闭。
这些 Context Pack 是主应用组装 Query/Planner/Repair 模型请求时的输入；它们不改变
确定性 Verifier 的最终权限。

## 单次运行的模型证据门

主应用已完成接线，但某一次运行只有在以下事实同时可见时，才能宣称“该 live run 使用了真实 LLM”：

1. 从 `Settings.model_client_config()` 构造真实 client/router；
2. Query/Planner/Repair 的调用 trace 记录非 scripted provider/model；
3. 预算化 Context Pack 真实进入 `ModelRequest.messages`；
4. 模型只提案候选、工具计划或修复策略，不能覆盖 Verifier、用户硬偏好或授权门；
5. 有真实模型的非回放评测，并单独报告成本、延迟、结构化输出失败和 fallback。
