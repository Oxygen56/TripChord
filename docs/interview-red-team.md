# TripChord 尖锐面试红队清单

这份清单不追求“每个问题都回答项目已经完美解决”。它把回答分成三类：

- **已实现**：有源码、测试或真实只读证据；
- **有意保留的硬边界**：继续改成 LLM 反而降低正确性或安全性；
- **尚未声称**：缺少供应商权限、真人数据或生产部署证据，必须主动说明。

## 先把三个总问题说清楚

### 当前到底用哪个模型？

安全默认是 `MODEL_PROVIDER=none`，所以克隆仓库后不会偷偷产生付费请求，也不能把默认
运行说成“正在使用 LLM”。项目支持 Anthropic Messages 和 OpenAI-compatible 网关；
2026-08-04 已用 DeepSeek `deepseek-v4-flash` 真实完成固定三请求 JSON/tool-loop smoke。三日期
Round 17 又记录 job-bound 47/47 成功模型调用和三个 checkpoint，但 runner 仍为
`done_gate_failed`；Evidence Arbiter policy 冲突修复后的同日期 focused run 为 23/23 成功，仍因
住宿精确报价平台 1/2 而 `HUMAN_BLOCK`。这说明“模型参与、模型调用成功”都不等于
“Done-Gate 通过”。正式演示应设置
`MODEL_AGENTS_REQUIRED=true`，任一必需模型阶段失败都会阻塞，而不是悄悄退化成脚本规则。

### 上下文、记忆和 RAG 是什么？

- 当前事实先进入带来源、版本、采集时间和过期时间的 Evidence Blackboard；
- Query / Planner / Repair 分别使用 1600 / 4000 / 3000 token 的按角色 Context Pack；
- 工具回执和历史记忆共用同一预算，超长回执只留下预览、原始字节数和 SHA-256；
- 记忆按 tenant、user、session、trip、TTL、隐私和可见角色隔离；用户长期偏好必须确认，
  可按 record id 撤销；匿名模式禁止写共享的长期用户记忆；
- RAG 是 BM25 词法检索，只检索偏好、历史决策、平台能力和非实时证据。实时价格、库存、
  余票永远不进 RAG，只能从本轮新鲜工具回执进入上下文。

没有使用向量库。当前可检索语料小、结构化字段强、还要求可解释过滤，BM25 是有意选择，
不是把向量 RAG 写漏了。

### 哪些由 Agent 决策，哪些必须是硬代码？

| Agent 真正拥有的决策 | 确定性代码保留的最终权威 |
|---|---|
| 需求语义提案与冲突解释 | 用户明示事实锁、缺字段阻塞 |
| 日期查询策略与探索/利用排序 | 日期全集、最大查询预算、最小覆盖 |
| 来源搜索优先级、波次和可选任务取舍 | provider capability、域名/工具权限、并发和速率上限 |
| 证据仲裁与候选策展 | 报价解析、金额、税费、币种、稳定身份和新鲜度 |
| 软风险批判 | Hard Verifier 与 ReVerifier |
| Repair 策略、候选换选、依赖刷新建议 | Repair Executor 校验目标 ID、差异与预验证 |
| 修复后 ReCritic | 最终 Safety Gate |
| Event Diagnoser 对事件语义 diff 的局部/全局/阻塞建议 | 单源重查范围、语义 diff、局部确定性 Repair/主 Verifier/异构 ReVerifier/事件安全门，以及全局预算预检 |
| 主控建议、解释、记忆候选 | 用户确认、租户隔离、持久化完整性和发布否决 |

面试时不要说“所有代码都是 Agent”。正确答案是：**Agent 对不确定语义和行动方案作能改变
执行的结构化决策；硬代码只负责不能被概率模型改写的事实、安全和发布门。**

## 架构与 Agent 合法性

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| 这不就是把函数改名成 Agent？ | 不是靠命名判断：模型 Agent 有阶段专属 goal、Context Pack、工具 allowlist、结构化输出和失败归因，并会改变日期顺序、来源波次、候选换选或事件路径；交通/住宿/iCom 则诚实标为固定单工具的 Source workers，不冒充自治 Agent。同一 Router/进程仍是共同故障域。 | 已实现；`agents/flexible_live_system.py`、`agents/live_system.py` |
| 为什么不把所有逻辑都交给 LLM？ | 金额、权限、约束和库存事实不能靠语言模型“判断大概正确”。把这些交给 LLM 会使系统不可复算、不可拒绝。 | 有意硬边界；`planning/package.py`、`agents/tools.py` |
| 主控 Agent 权限最大，能否覆盖 Verifier？ | 不能。主控只消费绑定候选 ID、版本、组件和错误码的 handoff；接受提案必须精确绑定 Repair/ReVerifier 的实际最终候选，evidence_ref 只能来自该候选。未知 ID 或无关证据即使在 advisory 模式也由 Safety Gate 阻塞。 | 已实现/反例测试；Planner→Verifier→Repair→ReVerifier→ReCritic→Orchestrator→Safety Gate |
| Agent 的建议到底有没有实际作用？ | API trace 同时记录 proposal、sanitized/applied action 和拒绝理由；不是只生成解释文字。 | 已实现；Agentic trace 与前端运行回执 |
| 模型失败后是不是偷偷走规则还声称用了模型？ | `MODEL_AGENTS_REQUIRED=true` 时失败关闭；可选模式才允许带 fallback 原因的确定性降级。 | 已实现；`agents/model_gateway.py`、`agents/live_advisory.py` |
| Safety Gate 后的解释/记忆 Agent 失败会不会漏出接受结果？ | 不会。最终 deterministic Publication Gate 依赖 Explanation 和 Memory Curator；required-model 模式下任一必需阶段失败都转为阻塞。 | 已实现/晚失败反例测试；`agents/live_system.py`、`tests/test_live_agent_system.py` |
| 解释 Agent 会不会编造“含早/免费取消”？ | 选择理由、权衡和权益陈述必须逐条绑定最终组件 ID 与 evidence_ref；运行时还会对早餐、行李、免费取消和含税等结构化权益做确定性对账。 | 已实现/未知组件与虚假含早反例测试 |
| 多 Agent 一定比单 Agent 好吗？ | 没有这个结论。当前公平 scripted 对照不能证明架构质量优势；可证价值是权限隔离、并发等待重叠、阶段化复核和可审计 handoff。同一 Router/进程仍有 common-mode failure。 | 声明边界；`docs/benchmark-agent-architectures.md` |
| 用的是 LangGraph 吗？ | 没有依赖 LangGraph。项目实现的是 typed Dynamic DAG runtime，因此简历只能写 “LangGraph-style”，不能写“基于 LangGraph”。 | 已实现自研 runtime；`agents/runtime.py` |
| 用了 MCP 吗？ | 当前 live 工具走内部 JSON Schema `ToolRegistry` 与最小权限 allowlist，不是 MCP。浏览器桥是本地租约协议，也不能包装成 MCP。 | 尚未声称 MCP |
| LLM 如何调用工具？ | 模型先返回受 schema 约束的 tool call；runtime 校验 Agent role、工具 allowlist、权限级别、参数和调用上限，执行后把回执作为不可信数据放回同一 Context 预算，再进行下一轮模型请求。 | 已实现；`agents/model_agent.py`、`agents/tools.py` |
| 动态 Agent 是不是让 LLM 自己无限 spawn？ | 不是。确定性 `ScaleDirective` 按工作量计算实例与并发 ceiling，`AgentTemplatePlan` 只分配白名单模板，运行时 ledger 在模型阶段前再次准入；ReAct Agent 只在获准分片内选合法 ID。 | 已实现控制器、模板与日期分片；不声称自由 Meta-Agent 自我复制 |
| 96 个 Agent 是性能最优解吗？ | 不是。96 是防请求自放大的 hard guardrail；最大 synthetic 输入提出 143 时被截断或提前拒绝。真实 SLA/成本调优尚无数据。 | 已实现硬门；`adaptive_control.py`、冻结预算基准 |
| 你说的 Agent 数到底是哪一个？ | `raw_logical_agents` 与 `logical_agent_count` 只计划 Flexible 阶段；自然语言入口的 Requirement Agent 先以 `CONTEXT` 角色进入同一本 ledger。规划实际增量是 `final admitted - scope start`，整个请求累计是 `final admitted`，后者必须 ≤96；stage、tool-loop request、HTTP attempt 和 Source worker 另算。 | 已实现 scope-start 双口径；禁止把规划计划、请求累计或浏览器 worker 混计 |
| Publication fallback 会不会绕过 96 上限？ | 不会。首批及每次额外刷新前都按累计尝试数重派生 directive/template；每次计 8 个基础模型 Agent，最多 8 次，再加已审计 Candidate Scout 增量与 scope/request ledger 对账。下一次容量不足时，在其浏览器/模型调用前停止，写 `publication_refresh_shortfall` 并最终 `HUMAN_BLOCK`。 | 已实现本地 fixture/反例测试；尚无真实 OTA 8 次 fallback 证据 |
| 模型并发 12 会不会把 Chrome 也扩到 12？ | 不会。12 是 lifespan 共享模型 HTTP pool 的进程级上限；请求内门从 1–2 起步、成功加一、失败减半。Chrome 仍固定 6，去哪儿住宿 1，日期对顺序准入 1。 | 已实现/并发测试；不同资源池不联动 |
| OTA 页面 pending 会不会让 LLM 并发减半？ | 不会。`provider_health` 与 `model_endpoint_health` 分离；前者决定 Source/覆盖诊断，后者和模型调用结果影响模型门。两者不得相互伪造。 | 已实现数据合同；当前执行前未观测 Provider 为 `unknown` |
| Candidate Shard 已经并行跑了吗？ | 已进入 live 决策代码并通过本地集成测试。Planner 有界池最大 256，`C>32` 后按 32 分成最多 8 个可并发调度、服务端绑定的只读 Scout；确定性 Collector 收敛到 `<=32` frontier，Evidence Arbiter 审核后只有唯一 Merger 能更新初案。Scout 不能写共享候选状态或越出分片 ID；实际同时在途数仍受自适应模型并发门约束。 | 代码 + 65 候选 32/32/1 structured-model/fixture 测试；真实 OTA 尚未触发 `C>32` |
| 那是不是已经穷举 2,000 个候选？ | 不是。2,000 只是 offline synthetic controller 允许的 `C` 预算算术输入，用来验证 96 Agent 饱和与拆分拒绝。live Planner 最多产生 256 个有界候选，而且上游 beam/prescreen 已截断原始组合。 | 明确声明边界；不把离线算术当 live/OTA 证据 |

## 日期搜索、缓存和组合空间

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| 8 月样本太少，为什么不穷举？ | 31 天 × 4 种住宿时长只有 124 个日期对，先低成本完整枚举粗粒度宇宙；昂贵的浏览器精查限制为 1–8 对。Query Agent 可在硬预算内真实重排，默认 acquisition 按该顺序执行 bounded Top-K。冻结 synthetic 基准显示旧 adaptive 总体输给 Top-K，guarded hybrid 在新 4–7 晚 sealed holdout 也未过门，因此 adaptive 只保留为显式注入实验项；这不证明 Top-K 在真实 OTA 上更优。 | 已实现并披露负面结果；`planning/adaptive_dates.py`、`agents/flexible_live_system.py`、`docs/date-search-hybrid-v2.md` |
| 只查 3–8 对会错过最低价吗？ | 会有可能，所以输出必须标“抽样、未穷举”，不能声称全月最低。离线 full-universe oracle 基准只衡量 recall/regret，不给线上最低价保证。 | 有意声明边界；日期搜索 benchmark |
| Agent 会不会一上来只查 1 对就停止？ | 用户/系统的硬预算和最低观察门约束 Agent；模型不能创建日期、扩大预算或绕过停止门。 | 已实现/测试 |
| 既然强调并发，为什么不把 8 个日期全并发？ | 每个日期对内部的独立 Source worker 尽量并发并由最多 6 个 Chrome lease 承载；日期对之间顺序准入，避免后排任务在队列中等到前排报价过期，导致一个整包混用不同新鲜度窗口。 | 有意的分层并发边界 |
| 缓存会不会把旧价当新价？ | 只复用同租户、同用户分区、完全相同查询的 10 分钟新鲜报价；半开 TTL，到期即失效；事件重查绕过复用。 | 已实现；`providers/browser_bridge.py` |
| 两个相同请求会不会重复打开页面？ | 同分区、同查询、显式允许复用时使用 single-flight，共享一个 bridge task；任一等待者超时不会取消其他消费者。 | 已实现/并发测试 |
| Source 全完成为什么还能失败？ | Source execution 只要求每个 required task 到类型化终态；exact quote coverage 另要求所选住宿每分段至少 2 个不同 provider 的 `QUOTE_FOUND`。`confirmed_empty`/bounded/pending 不贡献价格。 | 已实现/最新 focused run 为 execution complete、price coverage 1/2、`HUMAN_BLOCK` |
| 为什么不用 Redis 分布式缓存？ | 当前 live run/cache 是单进程本地参考实现并已明确披露；多 worker 需要 Redis/Postgres 锁、租约和幂等迁移，当前不能声称已支持。 | 尚未声称；runtime endpoint 明示 `multi_worker_supported=false` |
| 机票 × 酒店 × 接驳组合不会爆炸吗？ | 先按分段/provider/权益做确定性 beam 预筛，再以 candidate cap 和 transfer beam 截止；返回 raw/prescreen upper bound、生成数量、是否截断和证明 hash。 | 已实现；`PackageCandidateGenerationAudit` |
| 400 个日期是否又塞进了一个超长 prompt？ | 没有。每 12 行一个 scout；400 行是 34 个 scout，胜者经 3 个中间 merger 压缩，再由 1 个最终 merger 在 1–8 对硬预算内裁决，每个节点最多观察 12 行。 | 已实现日期两级树形归并与 400 日期集成测试 |

## 多平台报价与旅行产品边界

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| 支持平台 A 买机票、平台 B 买酒店吗？ | 支持。整包候选可以混合不同平台的往返机票、住宿和接驳。 | 已实现；真实只读证据曾形成去哪儿机票 + 携程住宿 + iCom 接驳 |
| 支持去程 A、返程 B 两张独立票吗？ | 当前不支持 split-ticket 航班；机票组件必须是一个完整往返组合。拆票涉及行李直挂、误机责任和自转机风险，需要独立产品模型。 | 尚未声称 |
| 不同 App 的价格真的可比吗？ | 只有人数、日期、房型/分段、币种、税费口径和权益字段对齐后才进入同一比较层；未知值不补成“已含”。 | 已实现；normalizer + Verifier |
| 同一个产品如何跨平台去重？ | 使用 product identity 与 offer identity 两级稳定身份，分别表达“像同一产品”和“权益/价格合同相同”；置信度不等于可订真值。 | 已实现；`planning/offer_semantics.py` |
| 三个平台不是同一秒抓，比较公平吗？ | 候选组件抓取时间差超过默认 20 分钟会被 Verifier 拒绝。 | 已实现；`QUOTE_CAPTURE_SKEW` |
| 住宿“没报价”到底有几种？ | 四态：`QUOTE_FOUND` 命中精确价；`CONFIRMED_EMPTY` 为 receipt-v2 双观测空结果；`BOUNDED_NO_EXACT_QUOTE` 为扫描上限内未命中；`BOUNDED_PROVIDER_PENDING` 为平台仍在搜索。 | 已实现；只有第一态进入价格比较 |
| `confirmed_empty` 会不会把瞬时抖动当空房？ | 要求同查询、同 tab/window/runtime lineage 的两次 parser-v1 观测，至少间隔 2 秒，分别有时间戳/canonical SHA；Bridge、normalizer、Done-Gate 独立重算。 | 已实现/篡改、间隔、query、tab、runtime 反例测试；仍不代表平台永久无库存 |
| 缓存价能否和新抓价混在一个方案？ | 只有都在 TTL 内且捕获偏差门通过才行；事件路径默认绕过复用。 | 已实现 |
| 能保证最低价、可订或锁库存吗？ | 不能。它只比较获准来源在本次账户、设备、日期和时点可见的报价；下单前仍需回官方页面重验。 | 永久声明边界 |
| 会员价、优惠券、设备价怎么办？ | 作为环境/权益字段记录；未知时不跨层比较。系统禁止领取或使用优惠券。 | 已实现边界 |
| 为什么没有 12306？ | 没有获准的官方实时余票接口，不能用未授权抓取冒充接入。 | 尚未声称 |

## Verifier、Repair、ReCritic 与动态重规划

### 面试时如何解释它们的“提示词与输入”

Repair Strategist 的系统约束不是一句“请修好行程”，而是：只能根据完整 Verifier 错误码、
Risk Critic 风险、候选摘要和本轮工具回执，在 schema 内选择
`SWITCH_CANDIDATE / EXPAND_SEARCH / ASK_USER / KEEP`；不得伪造 candidate id、改报价、
删错误码或宣布验证通过。输入包含被拒候选、冻结候选列表、组件 diff、需要刷新的依赖和
证据引用。

ReVerifier **没有 LLM 提示词**。它也不复用主 `PackageVerifier` 或 `diff_packages`；第二套
声明式不变量引擎从意图、修复前后候选和 Repair receipt 独立重算 13 类不变量，并生成绑定候选
ID、版本、组件集合、验证时间与失败码的审计 handoff。它与主 Verifier 仍共享业务语义，因此是
异构实现与故障隔离，不是形式化证明。故意把 ReVerifier 说成“再问一次模型”或“把同一函数再跑
一遍”都会暴露没有理解项目。

ReCritic 才是第二个模型阶段。它接收原 Critic 风险、Repair diff、ReVerifier handoff 和修复后
候选摘要，只回答软风险是否仍存在、证据是什么；即使它说风险消失，Safety Gate 仍要求明确的
resolved 证据，不能把原风险静默清零。具体 schema 在 `agents/live_advisory.py`，组装和执行在
`agents/live_system.py`。

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| Verifier 是不是 LLM 看一遍？ | Hard Verifier 是确定性规则，检查金额、时间、分段、证据、新鲜度、可比性、偏好硬约束和接驳；LLM Risk Critic 只补充软风险。 | 已实现 |
| Repair 是不是重新问同一个 LLM？ | Repair Strategist 读取完整错误码、Critic 风险和冻结候选摘要，输出 `SWITCH_CANDIDATE / EXPAND_SEARCH / ASK_USER / KEEP`；确定性 Executor 校验 ID、差异和预验证。 | 已实现 |
| Repair 后为什么还要 ReVerifier？ | Repair 可能修一个问题又引入另一个问题；异构 ReVerifier 用第二套实现独立重算 13 类不变量并绑定修复候选版本。即使主 Verifier 被 stub 为通过，金额、父链、diff、空接驳或硬偏好篡改仍会被拦截。 | 已实现/反例测试；`planning/package_reverification.py` |
| ReVerifier 过了，为什么还要 ReCritic？ | 硬规则通过不代表脆弱性消失；ReCritic 只看修复后候选、原风险和 ReVerifier handoff，必须明确说明风险是否消除。 | 已实现 |
| LLM 指向不存在或未展示候选怎么办？ | 目标必须来自只读工具展示的冻结候选集；未知、越权、无实质差异或预验证失败全部拒绝。 | 已实现/反例测试 |
| 动态重规划由什么触发？ | 手工事件、周期只读重核价、用户改需，以及可接入的外部事件信封；事件先做语义 diff，再由 Event Diagnoser 建议局部、刷新、全局或阻塞。 | 已实现前两类 |
| 如何监听平台价格变化？ | 当前没有供应商 webhook。用户显式开启后，本机进程按间隔轮询当前整包组件，每轮只重查一个，形成 `PRICE_CHANGED` 事件并走同一闭环。 | 已实现；`agents/live_monitor.py` 与 UI |
| 这能叫实时监听吗？ | 不能叫 push 实时监听，应叫“opt-in 周期性只读重核价”。进程重启后需要重新开启。 | 有意声明边界 |
| 局部事件会不会再跑整套模型 Critic/Repair/Orchestrator？ | 不会。browser local 与 iCom local 唯一可调用模型的事件阶段是一个 Event Diagnoser，局部 directive 为 `E=true、R=false、raw=1`；Repair、主 Verifier、异构 ReVerifier 和事件安全门全部确定性执行。 | 已实现/fixture 测试；不能把 normal pipeline 的模型 Agent 计入 local event |
| 局部修复和全局重算如何选？ | Event Diagnoser 提案，确定性影响域和证据门校验；依赖超出局部范围时才进入 global，关闭近期报价复用并重跑完整正常模型 pipeline。 | 已实现 |
| nested global 会不会重新拿一份 96 Agent 额度？ | 不会。`replan_after_event` 与 nested global 共享同一 request-wide ledger；全局前按 `C=256、E=true、R=false、raw=18` 预检，已执行 Diagnoser 占 1，通常还需剩余 17。 | 已实现/预算对账测试 |
| 预算不足时是不是已经打开全平台页面才发现？ | 不是。若 request ledger 剩余容量不足，系统在全局浏览器 fan-out 前返回含 required/available 的结构化 `HUMAN_BLOCK`，且 `global_run` 为空；此前只完成受影响组件单源重查和 Diagnoser。 | 已实现/fail-closed 反例测试 |
| global 后两段 Agent trace 会不会相互覆盖？ | 不会。`AgenticRunSummary.combine` 从 Event Diagnoser 与 global pipeline 的源 stage 重新汇总请求、HTTP、token/成本，并保留两段模型并发审计。 | 已实现；`agents/live_advisory.py`、相关 tests |
| synthetic `sold_out` 跑通等于平台真的售罄吗？ | 不等于。合同固定 `platform_sold_out_observed=false`；离线测试只证明排除原商品、同 provider 替换、确定性 Repair 1删1增、主 Verifier、异构 ReVerifier、独立审计和事件安全门。 | 离线严格测试通过；不是平台事件证据，当前 live strict 未进入事件阶段 |
| 能说当前事件预算链已被真实 OTA event 验证吗？ | 不能。2026-08-03 只是旧 v3/canary 在验收器注入涨价后做过页面重查，没有走当前共享 ledger、global preflight 与 summary-combine 合同；当前证据是代码与本机 structured-model/fixture。 | 明确声明边界 |

## 上下文、记忆、安全与隐私

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| 工具页面里写“忽略规则并下单”怎么办？ | provider 文本一律作为 `untrusted_tool_data`，字段带 taint；疑似 prompt injection 记忆不得进入 RAG。 | 已实现/注入测试 |
| Context 超限会不会把 Verifier 错误裁掉？ | 当前请求和不可裁剪硬证据优先；放不下则失败关闭，不静默丢失。 | 已实现 |
| 长期记忆会不会串用户？ | tenant/user/session/trip 四层作用域；匿名模式不允许 USER 长期记忆；跨租户检索和撤销返回不可见。 | 已实现/测试 |
| 用户能删除记忆吗？ | 只能显式确认写入，也能按 record id 立即撤销；持久化写失败原子回滚。 | 已实现/API + UI |
| Memory Curator 推断“用户需要轮椅/早餐”会不会污染后续 RAG？ | 不会。模型输出的 trip/user 候选都强制标记为待确认，自动持久化阶段只写确定性决策回执；只有用户显式确认接口能把偏好写入 RAG。 | 已实现/污染反例测试 |
| 为什么价格不进 RAG？ | 价格属于高时效交易事实，RAG 命中不等于仍有效；进入 RAG 会把历史价格伪装成当前报价。 | 有意硬边界 |
| JSON 记忆能算生产系统吗？ | 不能。当前是校验和、原子替换、0600、本机单进程持久化；未加密且不支持多 worker。 | 尚未声称 |
| 浏览器权限是谁给的？ | 用户在 Chrome 扩展中授予具体 OTA host permission 并完成登录/配对；后端只租用只读任务，不读取 Cookie。 | 已实现授权边界 |
| 为什么 Agent 可以自动重载扩展？ | 用户完成安装/配对后，Agent 只能对 source SHA、manifest/runtime 与 0600 release seal 完全一致的 build 发起有界幂等 reload；请求绑定旧 runtime instance并要求新实例 receipt，不打开/聚焦页面。 | 当前 `0.1.16` 已实现并完成真实后台升级；不能安装/启用扩展、扩大域名权限、恢复登录或绕验证码 |
| 会不会绕验证码？ | 不会。验证码/登录失效结构化失败并停下，用户恢复后才能重试。 | 已实现 |

## 评测、训练与工程可信度

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| 240 条是不是自己生成、自己判？ | 是固定种子合成任务，只证明合同内机制；不能外推真人满意度。 | 声明边界 |
| 预算基准四组全过，能否证明动态 Agent 更强？ | 不能。`adaptive-agent-budget-v1` 没有模型、浏览器或 OTA 调用，只冻结 Flexible 规划口径的 8/19/57/143→8/19/57/96、2/6/8/12 ceiling、Chrome 6、去哪儿住宿 1 与同输入复现；不含文本 Requirement admission。 | 只允许“确定性预算回归通过”；不允许全请求 Agent 数、质量、延迟、覆盖或 SLA 结论 |
| 单 Agent 对照公平吗？ | 历史 75% 是单候选确定性代理，不是 one-shot LLM；新的同输入/同最终 Safety Audit scripted 对照两者都 100%，所以不宣称多 Agent 质量胜出。 | 已修正 |
| 95% reranker 是否标签泄漏？ | 特征与 deterministic oracle 公式耦合，属于公式蒸馏；报告显式给出 closed-form oracle 100%，不包装成新规律发现。 | 已审计 |
| SFT/DPO 数据是否把答案塞进 prompt？ | 当前数据合同检查 rejection、label_source、oracle_action 等泄漏字段，跨 split 做 template overlap 和 tokenizer 长度审计。 | 已实现 |
| LoRA 真训练了吗？ | 两类 135M 模型各跑通 3 optimizer-step SFT→DPO 和 adapter reload；只算训练管线 smoke，未接 live。 | 已实现但不声明质量收益 |
| 为什么模型这么小？ | 为在本机验证数据、训练、保存和加载管线；不是用来证明中文旅行规划质量。 | 声明边界 |
| 模型调用次数怎么算？ | 分 stage、model stage、logical request、primary/fallback/HTTP attempts；tool loop 和重试不再混为一个数。 | 已实现/API + UI |
| 成本数字可靠吗？ | 只在供应商返回 usage 且配置单价时估算；没有 usage 不伪造成本。 | 已实现 |
| 测试通过等于生产了吗？ | 不等于。代码存在、测试通过、本机复现、真实外部只读证据、生产采用五层分开讲。 | 声明账本 |
| 当前最终 Done-Gate 通过了吗？ | 没有。三日期 Round 17 的异步 job `succeeded/complete`、47/47 模型调用成功，但 runner 是 `done_gate_failed`；policy 修复后的 focused strict 仍因住宿价格 1/2 而 `HUMAN_BLOCK`。去哪儿为 pending；同程 canary 曾遇登录门，用户已明确跳过该住宿来源。跳过不等于通过，当前仍无第二个价格。 | 当前真实状态；2026-08-03 文件只称历史 v3/canary |

## 独立运行与外部依赖

TripChord 源码没有 Codex 或 ChatGPT runtime 依赖，`/api/v1/agents/runtime` 也会返回两者为
`false`。但“只需要一个 LLM API Key”仍然是错误说法：

1. 回放/公共 API 路径可以独立运行，不需要 Codex/ChatGPT；
2. 模型多 Agent 需要一个受支持的 LLM endpoint 与 key；
3. 真实 OTA 路径还需要本地 FastAPI/browser bridge、Chrome Companion、用户授予的域名权限、
   有效登录态、网络和当前 DOM 合同；
4. 它不需要 Codex/ChatGPT 来代替浏览器 Agent，也不会把本次开发会话当成运行时组件。

## 额外架构攻击面

| 尖锐问题 | 当前回答 | 状态 / 证据 |
|---|---|---|
| Agent 之间自由聊天吗？ | 不。事实走版本化 Blackboard，阶段间走绑定 ID/version/component/error/evidence 的 typed handoff；冲突由用户明示事实和确定性 envelope 裁决。 | 已实现 |
| temperature=0 是否保证复现？ | 不保证逐 token 一致；使用 schema、request digest、provider/model trace、冻结行为评测与可重跑 smoke hash 审计。 | 声明边界 |
| Orchestrator 能否接受不存在的候选？ | ACCEPT/例外接受必须绑定实际 Repair/ReVerifier 最终候选和该候选证据；未知 ID 或无关 ref 在 advisory/required 都会被 Safety Gate 阻塞。 | 已实现/反例测试 |
| Explanation 会不会编造早餐或免费取消？ | 用户可见事实 claim 必须绑定组件/evidence_ref，早餐、行李、取消和含税权益再与结构化字段对账；后置 Publication Gate 覆盖失败。 | 已实现/反例测试 |
| 一个平台挂了会不会假装“没库存”？ | strict 要求所有 required Source；degraded 只能跳过预声明 optional；登录/验证码/DOM drift/超时保留技术失败，不能改写成库存空。 | 已实现 |
| HTTP job `succeeded` 是否等于方案接受？ | 不等于。job 是控制面终态；业务仍须检查主控状态、exact quote coverage、recommended ids 和最终 `done_gate.passed`。 | 已实现/当前 focused run 即为 job 完成但业务 `HUMAN_BLOCK` |
| 47/47 模型调用都成功，为什么中间日期对仍会失败？ | 模型传输成功不等于模型输出满足本地业务 policy。Round 17 的 Evidence Arbiter 当时收到冲突的 schema/Repair 指令，确定性 validator 拒绝后按 required-model 合同隔离该日期对；规则修复后的同日期运行以 23/23 调用完成。 | 已定位、修复并真实复测；但双平台住宿门仍未过 |
| 多币种如何算总预算？ | confirmed subtotal 与 supplemental published base fare 分层；同币种基础价进入最低预算下界，未知 FX/税费不伪造 all-in。 | 已实现/反例测试 |
| 事件并发会不会旧版本覆盖新版本？ | stable identity + candidate 父链 + per-run lock + 影响域门；单进程内串行，跨 worker 尚未声称。 | 已实现/边界 |
| 为什么扩展而不是托管 Chrome Profile？ | 用户按域名授权扩展，后端不复制 Cookie/Profile；DOM 合同漂移失败关闭。用户授权不等于平台条款许可，生产仍需政策审计。 | 已实现/合规边界 |
| Source worker 是不是假 Agent？ | 固定单工具节点明确称 tool-bound worker；模型推理权位于 Search Supervisor 等语义/策略 Agent。 | 已修正文档边界 |

## 仍然不能包装成“已解决”的问题

- 全网/全月最低价、库存锁定、订单原子提交和跨平台退款；
- 供应商 push webhook 或真正 24×7 持久化监控；
- 多 worker 分布式 live cache 与 memory；
- 真实用户满意度、转化率和生产 SLA；
- split-ticket 航班、12306 实时余票和未授权海外酒店来源；
- LoRA 对真实规划质量的提升；
- “多 Agent 普遍优于单 Agent”的科学结论。
- 当前 strict Done-Gate 已通过，或历史 v3/canary 可以替代新合同；
- 自动 reload 可以安装扩展、增加域名权限、绕过账号安全门；
- synthetic `sold_out` 等于监听到平台真实售罄。
- 当前 `replan_after_event` 的共享账本、全局预算预检或真实 OTA event E2E 已完成 live 验证；
- 96 是实验得到的性能最优值，或 96 个 Agent 会同时请求模型/打开页面；
- Candidate Shard 已在真实 OTA 运行中触发 `C>32`，或已并行穷举 2,000 个/全网候选；
- 动态 Agent 的 synthetic 预算基准通过等于当前真实 OTA Done-Gate 通过。

这些不是面试失败点。真正会导致失败的是把它们说成已经做完，然后拿不出代码和证据。
