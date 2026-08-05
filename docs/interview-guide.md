# TripChord 面试讲解与压力问答

## 90 秒项目介绍

我做的是一个面向自由行的、证据驱动的多 Agent 决策系统，不是把酒店和天气 API 包进聊天框。
用户需求先被拆成完整粗日期宇宙；Query Strategist 在 1–8 对硬预算内安排精查顺序，Search
Supervisor 读取 provider capability、缓存、延迟和权限后提出 Source waves，校验通过的 waves
会真实改写 DAG dependencies。交通、住宿和公共接驳是固定单工具、固定权限的 Source workers，
不冒充自治 Agent；它们取得报价后，系统做确定性
归一化、稳定身份和有界组合。Planner 最多产生 256 个候选；超过 32 个时按 32 分片为
最多 8 个只读 Candidate Scout，确定性 Collector 收敛到最多 32 个 decision frontier，
Evidence Arbiter 审核后只有唯一 Candidate Merger 能写入初案。

最终链路是 Hard Verifier、Risk Critic、Repair Strategist、确定性 Repair Executor、ReVerifier、
ReCritic、Orchestrator 和 Safety Gate。LLM 负责语义、工具计划、方案取舍与返工策略；金额、
新鲜度、权限、硬约束和发布否决始终由确定性代码负责。任何 Agent 都不能覆盖 Verifier。

Agent 数不是写死，也不是让模型自我复制：确定性 ScaleDirective 按工作量生成白名单计划，96 是
逻辑 Agent 硬上限；日期每 12 行交给一个 ReAct Query shard，超出单 Agent 上下文时经中间/最终
merger 树形归并。模型 HTTP 进程并发最多 12，请求内从 1–2 起步并按成功/失败加一或减半；Chrome
仍固定 6、去哪儿住宿 1、日期对 1。运行回执分开报告计划、获准与实际 Agent 数。
候选 Scout 也共享同一 ledger 和加一/减半并发门，不会因候选增多而放大 Chrome 并发。
自然语言入口还会先把 Requirement Agent 以 `CONTEXT` 角色记入同一本 request-wide ledger；
ScaleDirective 只计划后续 Flexible 阶段，scope start 用来区分规划增量与整个请求累计。
事件入口同样复用这本 96-Agent 账：local/iCom 路径唯一可调用模型的事件阶段是 Event Diagnoser，
Repair、主 Verifier、异构 ReVerifier 和事件安全门均为确定性；只有 global 升级才重跑完整正常
模型 pipeline，并在全局浏览器 fan-out 前以 `C=256、E=true、R=false、raw=18` 做容量预检。

模型层默认 `MODEL_PROVIDER=none`；项目支持 Anthropic 和 OpenAI-compatible 网关。2026-08-04
使用 DeepSeek `deepseek-v4-flash` 真实通过固定三请求 JSON/tool-loop smoke；三日期 Round 17
又以 job-bound 47/47 模型调用和三个 checkpoint 完成控制面，但 strict runner 为
`done_gate_failed`。中间 pair 的 Evidence Arbiter policy 冲突修复后，同日期 focused run 以
23/23 模型调用完成，所选住宿仍只有 1/2 个不同 provider 给出精确报价，系统经完整闭环输出
`HUMAN_BLOCK`。这说明模型真实参与、Source 执行
完整和最终可发布是三件事。上下文按 Query/Planner/Repair 分预算，RAG 是 BM25 词法检索，实时
价格和库存永远只来自本轮工具回执。

评测上我刻意保留负结果：公平 scripted single-vs-multi A/B 中两者都是 100%，多 Agent 更贵，
所以我不宣称质量优势；旧 adaptive 日期策略也总体输给 Top-K，因此降级为实验项。当前 strict
三日期 Round 17 已是新的 3600 秒异步/checkpoint sealed live run；它证明控制面与日期对隔离，
同时也明确证明当前双平台住宿证据门未通过。2026-08-03 两推荐日期与注入涨价局部重查只作为
历史 v3/canary，不代表当前 gate、
库存锁定、自然价格推送、可下单或全网最低。

## 五分钟展开顺序

1. 先讲“用户需求 → 日期预算 → Search Supervisor → 多源报价 → 有界候选”的数据流；
2. 再讲 Agent 与确定性权威边界，重点画出 Repair/ReVerifier/ReCritic/Safety Gate；
3. 用旧 adaptive 失败和 fair single-vs-multi 平局说明自己没有挑结果包装；
4. 展示真实 Chrome 证据与证据层级；
5. 最后主动说未完成的生产边界，让面试官没有机会用一句“这只是 demo”击穿全部叙事。

## 动态 Agent 必讲的八个问题

### A. 固定数量还是根据需求动态调整？

答：采用“固定硬边界 + 动态需求量”，不是二选一。角色、工具权限、96 个逻辑模型 Agent 上限、
模型 HTTP 进程上限 12、Chrome lease 6 和去哪儿住宿 1 都是不可突破的固定边界；具体日期分片数、
归并节点数和请求级模型并发 ceiling 才根据 `D/C/G/R/E` 与本轮 pipeline 工作量确定。真正执行
日期选择的仍是会调用 `inspect_date_search_space` 的 ReAct Query Agent，但它无权决定自己的
硬上限或任意创建角色。

追问：为什么不让一个 Meta-Agent 自己决定再创建多少 Agent？

答：Agent 自我复制会把成本、死循环和权限扩张都交给概率输出。当前做法允许模型决定分片内
取舍，却把实例数量、模板、工具和停止条件留给可复算控制器。通用 DAG runtime 虽支持受验证的
动态 spawn，但 production 日期树使用服务器生成的白名单任务，不把自由 spawn 包装成已上线能力。

### B. 96、12、6 这些数是怎么来的？

答：先说明哪些是设计 guardrail，哪些是工作量公式。每个日期分片最多 12 行、候选模板预留每
32 个一组，这是上下文可读性边界；模型理论并发按并行分片工作的平方根增长，再量化到
2/6/8/12。逻辑 Agent 的 96 是防止请求自放大的硬安全上限，不是通过真实流量求出的“最优点”；
最大 synthetic 审计输入会提出 143 个逻辑 Agent，控制器必须截到 96 或在无法安全执行时提前
拒绝。模型 HTTP 的 12 是进程级共享硬门，Chrome 的 6 和去哪儿住宿的 1 则来自执行资源/平台
隔离合同，模型数量不能带动它们扩容。

危险回答：不要说“实验证明 96 性价比最高”或“96 个 Agent 会同时请求模型/打开浏览器”。

### C. 计划、获准、实际 Agent 为什么是三个数字？

答：`raw_logical_agents` 是 Flexible 工作量提出的**计划需求**，允许超过 96；模板计划经过 hard cap、
白名单和原子分组后，`logical_agent_count` 是**获准计划**，其余进入 deferred；模型 Agent 真正
开始前还要通过并发安全的 `AgentBudgetLedger`，`admitted_count + admissions` 才是**实际执行**。
最后再用 Agentic trace 区分 stage、logical request 与 HTTP attempt。一个 Agent 的 ReAct tool-loop
会有多次请求，因此“实际 Agent=HTTP 请求数”是错的；13 个浏览器 Source worker也不是 13 个
模型 Agent。自然语言入口从 Requirement Agent 开始共享同一本账；它先以 `CONTEXT` 角色准入，
Flexible 入口再记录 scope start。规划实际数是 `final admitted - scope start`，整个请求累计是
`final admitted`。ScaleDirective 只覆盖前者，request-wide 96 硬门覆盖后者。
`replan_after_event` 与其中嵌套的 global run 也遵守同一口径：局部事件计划量是 1；global 最坏
预检计划量是 18，其中已执行的 Event Diagnoser 占 1，剩余流水线需从同一本账继续准入。

### D. 日期树形归并具体怎么工作？

答：完整粗日期宇宙先按每 12 行切片，各 Query Agent 独立调用只读检查工具并提名合法 ID；胜者
超过 12 个时，每组最多 12 个进入中间 merger，压缩后再交给最终 merger。在 400 日期上是
34 个 scout、3 个中间 merger、1 个最终 merger，共 38 个模型 stage；每个 Agent 最多观察 12 行，
最终仍只能给出 1–8 对精查日期。对应集成测试还审计两层并发门，不能把“树形归并”理解成
把 400 行重新塞回一个超长 prompt。

### E. Provider 健康和模型健康为什么必须分开？

答：OTA Provider 健康回答“携程/去哪儿某个垂类是否可搜索、strict 覆盖是否可能达到”；模型
endpoint 健康回答“LLM 请求是否应降低并发”。它们有独立字段。执行前没观测到的 OTA 状态是
`unknown`，不能因粗日历缺失就推断模型坏了；模型失败只让运行时并发门减半，也不能伪造成
平台无库存。报价能否发布最终仍看 Source receipt 和双 provider 精确报价门。

### F. 冻结基准到底证明了什么？

答：`adaptive-agent-budget-v1` 的 simple/standard/complex/audit 四组 synthetic controller state
把计划逻辑量冻结为 8/19/57/143，获准上限为 8/19/57/96，模型 ceiling 为 2/6/8/12；重复运行、
浏览器固定 6 和去哪儿住宿固定 1 均通过。它只证明同一输入的预算推导可复现、硬上限不漂移，
因为场景没有调用模型、浏览器或 OTA，不能证明动态 Agent 更聪明、真实延迟更低、价格更全或
当前 Done-Gate 已通过；它也没有覆盖文本入口的 Requirement admission，只冻结 Flexible
ScaleDirective 的规划口径。

### G. 发布重核价失败后继续 fallback，会不会偷跑出冻结预算？

答：不会把 fallback 当成免费重试。首批 publication refresh 和每个额外候选开始前，
系统都按“累计尝试数”重新生成 `ScaleDirective + AgentTemplatePlan`。每次发布重核价
预算 8 个基础模型 Agent，最多扩展到已冻结的 `exact_pair_budget=8`；再加上已实际发现的
Candidate Scout 增量，与 Flexible scope 和全请求 96 硬门同时对账。若下一次容量不足，
在该次浏览器/模型副作用前写入 `publication_refresh_shortfall`，停止继续尝试并
最终 `HUMAN_BLOCK`；不会先打开页面再报超限，也不会抛成 500。

追问：能说真实 OTA 已经验证连续 8 次 fallback 吗？

答：不能。现有可复现证据是 3 次逐次 refreeze、第 2 次容量不足且副作用计数不增加、
最多 8 次输入与模板容量的本地 fixture/回归测试；它证明 fail-closed 合同，不证明真实平台
会发生这样的失败序列。

### H. 候选分片也完成了吗？

答：已完成 live 决策链的代码与本地集成验证。Planner 有界池最大 256，Planner 结束后用
实际 `C` 重算 candidate-stage directive；`C>32` 时每 32 个一组，最多 8 个可并发只读
Scout。Scout 只能提名服务端绑定分片的 ID，确定性 Collector 生成 `<=32` frontier，
Evidence Arbiter 先审核 frontier 报价，再由唯一 `candidate_merger` 写 Planner 状态。
`C<=32` 仍走单 Curator 兼容路径。

追问：那能说真实 OTA 已并发处理 256 个候选吗？

答：不能。可复现证据是 65 候选的 32/32/1 分片、越权提名拒绝、伪造 hash 拒绝、
预算前置拒绝、唯一 Merger 写入和 Flexible 外层计数的本地结构化模型/fixture 测试。
当前没有真实 OTA 运行触发 `C>32` 的封存证据；2,000 只是离线合成控制器的预算
算术输入，不是 Planner 上限，也不是全网候选穷举。

## 尖锐问题与两层追问

### 1. 现在到底用哪个 LLM？

答：安全默认是 `MODEL_PROVIDER=none`，不会自动发起付费请求。网关支持 Anthropic Messages 和
OpenAI-compatible；当前最新实测模型是 DeepSeek `deepseek-v4-flash`。2026-08-04 的 runner
真实完成 3 次请求、1433 tokens、约 3.83 秒，JSON 与工具循环通过；面试演示若要保证模型真的
参与，还应开启 `MODEL_AGENTS_REQUIRED=true`。

追问：那你能说“TripChord 用 DeepSeek 做实时规划”吗？

答：可以说 DeepSeek V4 Flash 已通过网关/tool-loop smoke，并真实驱动过 Chrome 严格运行；最新
focused run 的必需模型阶段没有失败，但住宿精确报价平台只有 1/2，最终仍是 `HUMAN_BLOCK`。
所以只能证明真实接线、真实调用、失败归因和 fail-closed，不能升级成当前 Done-Gate 成功。

危险回答：不要说“默认就是 DeepSeek”“只要有 API key 就可以跑完全部流程”。

### 2. 项目是否依赖 Codex 或 ChatGPT？只需要一个 LLM key 吗？

答：代码和运行时不依赖 Codex/ChatGPT。回放路径不需要模型；真实模型 Agent 需要 endpoint/key；
真实 OTA 还需要本地 FastAPI/browser bridge、Chrome Companion、用户授予的 provider 域名权限、
有效登录、网络和兼容 DOM。LLM key 解决推理能力，不会自动获得用户浏览器登录态。

追问：谁给浏览器 Agent 权限？

答：用户安装扩展并对具体官方域名授予 host permission，配对本机 loopback bridge；后端只下发
只读搜索任务。扩展不把 Cookie 交给模型，也禁止订单、支付、优惠券、账号和验证码绕过动作。
安装/域名授权必须由用户完成；后续版本切换可由受限 reload 工具在后台完成，但只允许 source
SHA、manifest/runtime 和 0600 release seal 精确一致的目标，并验证 runtime instance 轮换。

危险回答：不要说“Agent 自己拿到了浏览器权限”“自动重载能扩大域名权限”或“使用 ChatGPT 的浏览器”。

### 3. 上下文工程到底做了什么？

答：本轮事实先进入版本化 Evidence Blackboard。Query/Planner/Repair 分别有 1600/4000/3000
token 的 Context Pack；优先保留当前请求和 Verifier 关键拒绝，再放新鲜证据和历史记忆，同时
为后续工具观察预留预算。关键证据放不下时失败关闭。大工具回执只保留显式预览、原字节数和
SHA-256，不在预算外无限拼接。

追问：这个 token 是精确 tokenizer 吗？

答：当前是可复现的近似预算，用于本地资源 envelope；模型 API 返回的真实 usage 单独记录。
不能把近似计数说成供应商 tokenizer 的精确证明。

危险回答：不要只说“截断 prompt”，要讲优先级、不可裁剪项、工具预算和失败关闭。

### 4. 记忆和 RAG 怎么设计？为什么不用向量库？

答：记忆分短期工作记忆、事件/决策记忆、用户偏好和平台能力，按 tenant/user/session/trip、TTL、
隐私和可见角色隔离。长期用户偏好必须显式确认，可撤销；匿名模式不写长期用户记忆。RAG 使用
BM25 词法检索，只取偏好、历史决策、平台能力和非实时证据；实时价格、余票、库存禁止进入 RAG。

追问：为什么 BM25 足够？

答：当前语料小、字段结构化且安全过滤比语义召回更关键，BM25 可解释、易复算。若未来语料扩大，
可以增加 hybrid/vector retrieval，但仍必须先执行作用域、TTL、隐私和实时数据禁入门。

危险回答：不要声称使用 Milvus/Chroma 或“把历史最低价放入知识库”。项目没有这么做。

### 5. 这不还是一堆硬代码吗，哪里是真 Agent？

答：判断标准不是代码量，而是模型是否拥有受限但真实的决策权。Query Strategist 能重排将要花钱
精查的日期；Search Supervisor 能改变 Source waves/dependencies；小池 Candidate Curator 或大池
Candidate Scouts + 唯一 Merger 能改变初案；Repair Strategist 能触发真实候选换选；Event Diagnoser
能改变局部/全局处置。每个阶段有独立
goal、Context、工具 allowlist、schema、失败域和 trace。

追问：为什么 Verifier、金额和权限不用 Agent？

答：这些是必须可复算、可测试、可拒绝的事实与安全不变量。LLM 可以解释风险，但不能“认为金额
大概对了”或“推测用户已授权”。硬规则不是 Agent 化失败，而是 Agent 的安全执行边界。

危险回答：不要说“尽量所有地方都用 LLM”。好 Agent 系统不是概率模型占代码比例最高。

### 6. Search Supervisor 只是输出一段调度建议吗？

答：不是。它必须先调用 `inspect_search_capabilities`，再返回含 waves、task IDs、预算和跳过项的
schema。确定性校验通过后，`materialize_search_schedule` 把 waves 转成每个 Source task 的真实
dependencies 和 priority。未知/重复 ID、漏必需任务、超预算、越权或 strict 模式跳过都会原子拒绝。

追问：模型把 11 个浏览器任务放一个 wave 怎么办？

答：wave 是逻辑 ready 集，实际并发仍受 Dynamic Scheduler 和 browser bridge 的全局资源上限
约束，当前最多 6 个浏览器任务并发。模型无权扩大底层 lease。

危险回答：不要说“17 个 Agent 就同时打开 17 个标签页”。当前 17 是 13 浏览器 + 4 iCom 的
逻辑 Source，且 tool-bound workers 不是自治 LLM Agent；实际 Chrome lease 上限仍是 6。

### 7. LLM 是怎么调用工具的？平台页面里的提示注入怎么办？

答：模型先返回 schema 化 tool call；runtime 检查 role、task allowlist、工具权限、参数和轮数，
执行后把 receipt 作为 `untrusted_tool_data` 放回同一 Context 预算。系统 prompt 明确平台文字和
报价字段都是数据而非指令；越权工具名直接拒绝，实时报价也不能被写入 RAG。

追问：prompt injection 能否保证 100% 防住？

答：不能承诺语言模型永不受影响，所以关键安全不依赖 prompt。即使模型被诱导，工具 allowlist、
只读 bridge、确定性 Verifier 和 Safety Gate 仍应拒绝越权动作；测试只证明已覆盖的攻击合同。

危险回答：不要说“系统提示足够强，所以不会注入”。

### 7.1 真实平台登录态、个人行程和报价会不会发给外部模型？

答：要分开说。启用外部 endpoint 后，预算化用户需求、结构化证据摘要和该 Agent 获准观察的
工具回执会发给模型，否则模型无法据此决策；这可能包含用户的行程偏好和报价摘要。TripChord
不把 Chrome Cookie、登录凭据、浏览器 profile 或 bridge pairing secret 交给模型，内部 trace
只存 prompt digest，不存 prompt 明文。

追问：那是否满足企业 DLP、境内存储或 GDPR/网安合规？

答：当前没有这种承诺。本地不落 prompt 明文不代表数据没有发往供应商；外部 endpoint 自己的
日志、训练、保留和地域政策仍适用。敏感场景应选择符合组织政策的自托管/合规 endpoint，或保持
`MODEL_PROVIDER=none`。企业级 DLP、字段级脱敏与地域合规需要独立设计和审计。

危险回答：不要说“模型只看到 hash”“外部 LLM 完全看不到用户行程”或“项目天然合规”。

### 8. 8 月只抽样几天，样本太少；穷举又太多，怎么解决？

答：先区分廉价枚举与昂贵精查。“玩 5–8 天”是住 4–7 晚，8 月共有 124 对，完整粗枚举并不多；
昂贵的是每对再做当前 13 路浏览器 + 4 路 iCom 真实查询，所以硬预算限制为 1–8 对。
Query Strategist 可以重排，默认
按该顺序 bounded Top-K 精查，输出明确写“抽样、未穷举”。

追问：为什么不用 adaptive exploration？

答：做过。旧 full-universe synthetic v1 中 adaptive 在预算 3/5/8 的 Recall@3 与 regret 总体
都输给粗价 Top-K；guarded hybrid 在新的 4–7 晚 sealed holdout 也没过不退化门。因此 adaptive
降级为实验项。Top-K 是当前保守默认，不代表已在真实 OTA 上证明最优。

危险回答：不要声称“智能抽样保证找到全月最低”。

### 9. 报价可以缓存吗？会不会用旧价误导用户？

答：可以，但只在调用方显式允许、同 tenant/user 分区、查询所有价格相关字段完全一致且距离
captured_at 小于 600 秒时复用；TTL 是半开区间，刚好 600 秒即过期。事件刷新绕过缓存，run trace
记录 reused_from_task_id 和 age。

追问：同时来两个相同请求呢？

答：使用进程内 single-flight，共享一个 bridge task。每个消费者独立等待；一个等待者超时只减少
消费者计数，不会取消其他消费者。跨租户、跨用户或未认证分区不共享。

危险回答：不要说“缓存 10 分钟所以 10 分钟内价格一定不变”。

### 10. 支持一个平台买机票、另一个平台买酒店吗？

答：支持，候选可以组合不同 provider 的完整往返机票、住宿和接驳。2026-08-03 的只读证据曾
形成跨来源组合。但当前不支持把去程 A、返程 B 拆成两张独立票；split-ticket 需要另建行李直挂、
误机责任、自转机和保障产品模型。

追问：不同 App 的价格口径怎么比较？

答：人数、日期、币种、税费、每人/整单、每晚/整住、房型、早餐、取消和支付条件要显式归一；
未知字段保持未知。整包抓取时差默认超过 20 分钟还会被 `QUOTE_CAPTURE_SKEW` 拒绝。

危险回答：不要把起步价、会员价、优惠券价和含税整单价放在同一层直接排序。

### 11. 同一航班/酒店跨平台如何去重？

答：使用 product identity 和 offer identity 两层。前者判断是否像同一航班/酒店产品，后者包含
舱位/房型、rate plan、早餐、取消等权益合同。DOM 临时 ID、价格和 observation receipt 不参与
稳定产品身份；价格变化要在同一 stable offer 上比较。

追问：官方 ID 缺失怎么办？

答：使用受限字段构造较低置信度身份并披露缺失项；置信度不能升级成库存真值。若关键字段歧义，
Verifier 可以拒绝跨平台等价比较。

危险回答：不要说“hash 一下页面文字就一定是同一个产品”。

### 12. 机票 × 酒店 × 接驳不会组合爆炸吗？

答：不会假装全量枚举。live 先把 flight 限为 12、每住宿段限为 8、每接驳合同桶限为 8，再用
transfer beam 64 和默认 candidate cap 256。审计回执保存 raw/prescreen 数量、结构上界、ID hash、
实际生成数量和截断状态。`C>32` 后全池被服务端完整分成最多 8 个 32 候选只读
Scout scope；确定性 Collector 只把最多 32 个送入 Evidence Arbiter 与最终 Merger。因此
不是一个模型看 256 个，也不是穷举全部原始组合。

追问：那最优候选可能被 beam 剪掉吧？

答：可能，所以输出必须声明 omitted scope，不能说全局最优。beam 以价格、类型、provider 和权益
覆盖保留多样性，目标是在硬资源预算内给 Agent 可解释选择集，不是证明穷举最优。

危险回答：不要用 candidate cap 后仍声称“遍历所有组合”。

### 13. Verifier 是 LLM 吗？提示词是什么？

答：Hard Verifier 不是 LLM，没有提示词。它确定性检查日期、时间、接驳、金额、报价状态、证据、
新鲜度、口径和用户硬偏好。LLM Risk Critic 只查红眼、自转机、取消条款缺失等软风险，并必须
引用本轮证据。

追问：为什么还需要 Critic？

答：硬规则必须稳定，但旅行脆弱性并不全是布尔约束。Critic 提供反方假设和可解释风险；它可以
触发 Repair，却不能把 warning 伪装成硬事实，也不能宣布方案通过。

危险回答：不要说“Verifier 就是让模型再检查一遍”。

### 14. Repair 和 ReVerifier 具体怎么工作？

答：Repair Strategist 的输入包含完整 Verifier error codes、Risk Critic 风险、冻结候选摘要、
组件 diff、依赖刷新要求和证据 refs；schema 只允许 `SWITCH_CANDIDATE / EXPAND_SEARCH /
ASK_USER / KEEP`。确定性 Repair Executor 再校验 target ID 是否已展示、是否真有组件变化、是否
需要刷新依赖以及预验证是否无 hard error。

追问：ReVerifier 的提示词是什么？

答：没有提示词。ReVerifier 也不是再调用一遍主 `PackageVerifier`：第二套声明式不变量引擎不
调用 `PackageVerifier` 或 `diff_packages`，从意图、修复前后候选和 Repair receipt 独立重算 13 类
不变量，覆盖金额/预算下界、版本父链、真实 diff、硬偏好、逐晚住宿、住宿结构、报价时效/
抓取偏差、接驳价格合同和接驳链，再产生绑定 candidate/version/
component set/时间的审计 handoff。它仍共享同一业务语义，所以是异构故障隔离，不是形式化证明。
ReCritic 才是第二个模型阶段，复审修复后的软风险是否真的消除。

危险回答：不要说“Repair 和 ReVerifier 都是让同一个 LLM 自省”，也不要说“独立实现等于数学证明”。

### 15. Orchestrator 权限最高，能推翻 Verifier 吗？

答：不能。Orchestrator 只消费完整 handoff，提出“直接接受 / 确认例外后接受 / 重新规划或暂停”
建议。最后还有确定性 Safety Gate；只要 hard violation、required model failure、未解决高风险或
handoff 不一致存在，就可以拒绝主控建议。

追问：用户明确接受风险能否绕过？

答：用户可确认可披露软例外，不能绕过金额错误、未授权工具、新鲜度、证据伪造等系统硬边界。

危险回答：不要说“主控 Agent 最终裁决，所以能覆盖任何模块”。

### 16. 动态重规划何时触发？怎么监听价格变化？

答：触发源包括用户手工事件、用户改需、验收/外部事件信封，以及用户显式开启的周期只读重核价。
当前 monitor 是本机进程内轮询，每轮只重查一个当前整包组件；事件形成 stable identity 绑定的
语义 diff，Event Diagnoser 再建议局部/全局路径，确定性代码限制 provider 和分段范围。

追问：局部事件是不是又跑一遍 Risk Critic、Repair Strategist、ReCritic 和 Orchestrator？

答：不是。browser local 与 iCom local 的唯一可调用模型阶段是一个 Event Diagnoser；
`ScaleDirective` 是 `E=true、R=false、raw=1`。之后的 Repair、主 Verifier、异构 ReVerifier 和
事件安全门都由确定性代码完成，不运行模型 Repair Strategist、ReCritic 或 Orchestrator。

追问：Event Diagnoser 要求全局重规划时，如何防止 96 个 Agent 预算被嵌套调用绕过？

答：`replan_after_event` 与 nested global 共用 request-wide ledger。启动全局浏览器 fan-out 前，
系统按 `C=256、D=0、G=0、E=true、R=false、direct-final=1` 做最坏预检，冻结 raw=18；
Diagnoser 已占 1，必须还能容纳其余 17。容量不足直接返回包含 required/available 的结构化
`HUMAN_BLOCK`，全局搜索不启动；通过后才禁用近期报价复用并重跑完整正常模型 pipeline。
最终用 `AgenticRunSummary.combine` 合并两段 trace，并保留各自的模型并发审计。

追问：已经监听到真实平台自然涨价或售罄了吗？

答：没有。2026-08-03 是历史 v3/canary 中由验收器注入 `price_changed` 后真实重查页面；当前
synthetic `sold_out` 也明确固定 `platform_sold_out_observed=false`。离线测试走通了排除原商品、
确定性 Repair 删除 1/新增 1、主 Verifier、异构 ReVerifier 和事件安全门，但这不是平台自然事件。
当前共享 ledger、全局预算预检与 summary 合并只有代码和本机 fixture 测试证据；历史 v3 页面
重查没有走这份新合同，不能说当前 OTA event 已验证。周期 monitor 也不是供应商 push，进程重启
后要重新开启，更不会锁库存。

危险回答：不要说“系统实时监听各平台价格推送”。

### 17. 多 Agent 比单 Agent强在哪里？

答：当前不能用质量数字证明更强。旧 75% 是单候选确定性代理消融，不是 one-shot LLM Agent。
后来做了同任务、同工具、同模型标识、同总预算和共同最终审计的 scripted A/B，single 与 multi
都为 100%，multi 使用更多调用、token、成本和延迟，`winner_claim_allowed=false`。

追问：既然更贵，为什么保留多 Agent？

答：价值是最小权限、上下文隔离、阶段化失败归因、结构化 handoff、独立反方复核和可并行的外部等待；
同一 Router、进程或模型供应商仍是共同故障域，
不是“角色越多分数越高”。是否值得应在真实模型/真实任务上继续评估，不能预设答案。

危险回答：不要说“多 Agent 从 75% 提升到 100%”。

### 18. 模型不可用时会发生什么？

答：可选模式可以使用确定性 fallback，但 run receipt 会显示 stage、实际 model stage、logical
requests、HTTP attempts 和 fallback reason。required-model 模式把缺失/非法模型提案标成发布
阻塞。搜索可以继续收集诊断，不等于最终可发布。

追问：调用次数怎么统计？

答：区分 Agent stage、实际 model stage、logical router request、primary/fallback/HTTP attempt；
tool loop 会让一阶段产生多轮 request，重试又会增加 HTTP attempt，不能混成一个“模型调用数”。

危险回答：不要拿 `model_call_count` 一个兼容字段解释全部成本。

### 19. 后训练真的有效吗？

答：只声明两层离线证据。合成编排任务上 Base/SFT/SFT+DPO 为 66.67%/100%/100%，DPO 没有
证明高于 SFT；恢复 reranker 的 95% 来自合成加权 oracle。SmolLM2-135M-Instruct 只跑了 3-step
LoRA SFT→DPO 与 adapter reload，用于验证训练工程链路，未接入 live。

追问：为什么不把 LoRA 写进核心简历数字？

答：样本小、训练步数极少、oracle 合成且缺少真实任务盲评。写成“中文规划质量提升”会被一问
即穿；更有价值的事实是我能说明数据合同、泄漏检查、训练/加载链路和停止声明的原因。

危险回答：不要说“LoRA 把行程质量提升到 95%”。

### 20. 真实运行到底证明了什么？

答：分新旧两层。2026-08-03 历史 v3/canary 在用户授权 Chrome 会话中形成两个推荐日期，并在
注入住宿价格事件后完成一次单源局部重查。当前三日期 Round 17 已走完异步控制面：47/47 模型
调用成功、三个 checkpoint 齐全，但 runner 为 `done_gate_failed`。其中一个 pair 的 Evidence
Arbiter policy 冲突随后已修复；同日期 focused run 以 23/23 模型调用完成，仍因住宿精确报价平台
1/2 而由主控输出 `HUMAN_BLOCK`。

追问：它没证明什么？

答：没证明当前 Done-Gate 通过、自然价格推送、全月/全网最低、库存锁定、可下单、真实用户效果、
长期 DOM 稳定、当前事件预算链的真实 OTA 验证或生产 SLA。历史成功包也不能抵扣新合同。

危险回答：不要把“真实只读一次通过”说成“生产上线稳定运行”。

### 21. 如果并发很多，报价会不会前后不一致？

答：并发分两层：同日期对内 Source 尽量并发，日期对之间串行准入。原因是 13 个浏览器 Source
已经能占满 6 个 lease；若三个日期对同时入队，早期报价可能等到其他日期执行后过期。Verifier
还会拒绝捕获时间差超过 20 分钟的整包。

追问：串行日期对是不是违背“把多 Agent 并发发挥到极致”？

答：不是，并发目标是缩短独立 I/O 等待且保持证据一致性，不是同时启动最多任务。跨日期串行是
新鲜度背压策略；如果未来有独立浏览器池或官方批量 API，再重新评估准入粒度。

危险回答：不要把最大并发数当作系统质量指标。

### 22. 搜索要几分钟，HTTP 超时、重复提交和取消怎么处理？

答：前端走异步控制面：POST 快速返回 `202 + job_id`，GET/SSE 展示
`queued/running/succeeded/failed/cancelled` 与阶段进度，DELETE 取消并把取消传播到实际 planning
task 和 browser bridge。终态按容量与 TTL 有界保留，外显错误只留异常类型和通用描述，避免把
用户需求、URL 或报价泄漏到错误文本。

追问：用户网络重试会不会启动两次昂贵搜索？

答：同 tenant 的相同 `Idempotency-Key + payload digest` 返回原 job；同 key 换 payload 返回 409，
跨 tenant 的 key 不共享。strict runner 也改用 tenant-scoped GET polling，记录单调 revision、阶段
进度和日期对 checkpoint；服务端总预算冻结为 3600 秒，客户端必须多留至少 30 秒。

追问：能否跨进程重启恢复？

答：不能。当前是进程内 bounded registry，重启不恢复 job，也没有多 worker 一致性或交付 SLA。
生产形态需要外部持久队列、分布式租约和共享幂等存储。

危险回答：不要把 POST 202、SSE 和 `Idempotency-Key` 三个接口名包装成“生产级任务平台”。

### 23. 当前最接近生产的缺口是什么？

答：模型+Chrome 统一 E2E、真实授权回放集上的日期策略校准、多 worker 的分布式缓存/租约、
持久化任务队列、平台 DOM 漂移监控、更多时间点的稳定性复测，以及下单前官方重验/人工确认。
当前 memory/live cache/monitor 都明确有单进程边界。

追问：为什么不继续假装 Compose 就是生产？

答：Compose 只证明本地部署配置存在。没有多实例一致性、故障恢复、OIDC、观测告警、容量与
供应商 SLA 证据时，不能写“生产级”。

危险回答：不要用 Docker、Redis、Nginx 三个名词替代生产验证。

### 24. 多个 Agent 怎么通信、冲突时谁说了算？

答：Agent 不自由群聊。当前事实进入版本化 Evidence Blackboard，跨阶段只传 schema 化 handoff，
其中绑定 candidate ID、version、component IDs、error codes、evidence refs 和生成时间。模型提案冲突
时先按用户明示事实与 Preference Constitution 处理，再由确定性 envelope 检查；未知 ID、陈旧版本、
证据越界或主控覆盖硬拒绝都会失败关闭。

追问：这还是多 Agent 吗，为什么不让它们互相辩论？

答：多 Agent 的价值是阶段专属目标、上下文、工具权限和失败归因，不是制造无限对话。自由辩论会
放大 token、提示注入和状态漂移；需要反方意见的地方已用 Risk Critic 与独立 ReCritic 明确建模。

### 25. 用户把早餐权重调得很高，能否覆盖 Agent 的低风险判断？

答：可以覆盖软排序，不能覆盖事实。早餐有“必须满足 / 按重要程度权衡 / 明确禁止 / 不作要求”
四态；required/forbidden 是 Hard Verifier 约束，weighted 才使用用户 0–1 权重，并且只在同一报价
证据层的可比候选中融合价格与早餐覆盖。Agent 无权改写用户明示模式或权重，未知早餐也不会被
猜成包含或不包含。

追问：权重 1 是否等于早餐一定有？

答：不等于。权重决定取舍强度，证据字段决定事实；只有 required + 明确权益证据才能作为硬满足。

### 26. temperature=0 就能保证模型可复现吗？提示词变了怎么审计？

答：不能保证逐 token 确定。temperature=0 只是降低随机性；每次调用保留 provider/model、schema、
request digest、usage、延迟与成功/失败 trace，复现 runner 还输出 request/response/schema/code hash。
冻结 benchmark 检查行为合同，而不是假装模型文本永远相同。提示词或代码变化后必须生成新证据，
不能沿用旧 DeepSeek artifact。

### 27. 主控、解释或记忆 Agent 幻觉怎么办？

答：主控接受建议必须绑定 Repair/ReVerifier 实际最终候选 ID，证据只能来自该候选；Explanation 的
选择理由、权衡和早餐/行李/取消/含税等权益陈述必须逐 claim 绑定组件与 evidence_ref，并由确定性
字段复核。Explanation 与 Memory Curator 后还有最终 Publication Gate。Memory Curator 的 trip/user
候选都只进待确认区，模型自由文本不会自动写入 RAG。

### 28. 为什么用 Chrome 扩展，不直接用 Playwright 复用用户 Profile？

答：扩展让用户在浏览器原生权限模型中按官方域名授权，并在现有登录页内执行只读查询；后端只拿
结构化回执，不复制 Cookie 或整个 Profile。直接托管个人 Profile 的侵入面和凭据风险更大。页面
解析遵循版本化 DOM contract；登录、验证码或 DOM drift 都结构化失败，不靠猜 selector 继续跑。
当前 `0.1.16` 支持受限后台 reload：只对 release-sealed 精确 build、绑定旧 runtime instance、无
活动 task lease 时执行，并要求新实例 receipt；整个协议不会打开或聚焦 Chrome 页面。

追问：用户授权就等于平台允许自动化吗？

答：不等于。用户授权只解决本机权限与意图，不替代平台条款或法律审查；项目限制为只读研究，
不绕验证码、不下单，生产采用仍需单独审核平台政策并优先使用获准官方 API。

### 29. 一个平台失败时，系统会不会拿剩余结果假装完整？

答：strict 模式要求所有预声明 required Source 达到类型化终态；degraded 模式也只能跳过能力表中
预先标为 optional 的任务。Search Supervisor 不能把 required 改成 optional。住宿结果再细分为
`QUOTE_FOUND / CONFIRMED_EMPTY / BOUNDED_NO_EXACT_QUOTE / BOUNDED_PROVIDER_PENDING`；只有第一态
贡献可比价格。输出同时携带 Source execution completeness 和 exact quote comparison coverage，
不把登录失败、DOM 漂移、超时、空结果或 pending 冒充第二个平台报价。

### 30. CNY 机酒加 USD 接驳，预算是怎么验证的？

答：确认含税且同币种的组件进入 confirmed subtotal；公开基础价作为 supplemental fare 单独展示，
未知税费和 FX 不会被伪装成全包价。若公开基础价本身与预算同币种，它会进入“最低已知预算下界”，
下界已超预算就直接拒绝；不同币种没有可信 FX 证据时只披露，不能算出虚假的总价。

### 31. 价格事件与用户改需同时到达，会不会用旧版本覆盖新版本？

答：事件绑定 stable component identity、candidate version 和 run tenant；同一 live run 通过进程内锁
串行提交，Repair 生成父链明确的新版本，事件刷新绕过报价缓存。陈旧 target/version 或越出影响域
会阻塞，而不是 last-write-wins 静默覆盖。当前锁与 run cache 仍是单进程实现，多 worker 需要共享
租约和一致性存储。

### 32. Source worker 也叫 Agent，是不是 agent-washing？

答：交通、住宿和 iCom 执行节点只是固定单工具、固定权限的 tool-bound workers；它们不被包装成
独立 LLM 推理 Agent。真正的模型决策在 Query Strategist、Search Supervisor、Evidence Arbiter、
Candidate Scout/Merger、Critic/Repair/ReCritic、Event Diagnoser 与 Orchestrator。Candidate 分片只有本地
结构化模型/fixture 证据，简历不把它写成真实 OTA 已触发。

### 33. 所有 Source 都完成，为什么主控仍然阻塞？

答：因为“执行完整”与“精确比价完整”是两条正交门。一个住宿 Source 可以合法结束为命中报价、
双观测确认空结果、有界未命中或平台仍在搜索；这说明系统没有丢任务，但只有命中精确报价才贡献
provider price。当前 strict 合同要求所选住宿每分段至少两个不同 provider 报价；最新同日期
focused run 只有携程 1 个，去哪儿为 `bounded_provider_pending`，所以必须 `HUMAN_BLOCK`。

追问：为什么不把 `confirmed_empty` 当成“去哪儿已参与比较”？

答：可以说去哪儿参与了来源执行与库存证据采集，不能说它参与了价格比较。没有价格就无法归一化
金额、税费和权益；把“无价的空结果”凑成第二个价格会直接破坏比较分母。

### 34. `confirmed_empty` 会不会是页面抖动造成的误判？

答：所以它不是单次 selector 结果。receipt-v2 要求同一精确查询、同一 tab/window/runtime lineage
下两个 parser-v1 观测，至少间隔两秒，分别绑定时间戳和 canonical SHA；Bridge、normalizer 与
Done-Gate 独立重算。任一查询指纹、间隔、tab、window、runtime 或 SHA 不一致都会降级/拒绝。
即便合同通过，它也只表示该次观察窗口为空，不代表平台永久或全量无库存。

### 35. 为什么能让 Agent 自动重载扩展，不会变成任意代码执行吗？

答：Agent 不能传文件路径、URL、hash 或脚本，只能给枚举 reason。目标由本地 release seal 推导，
必须与 source SHA、manifest/runtime/build metadata 完全一致，seal 还要求当前用户所有且权限为
0600。命令绑定当前 runtime instance、幂等键、TTL、冷却和重试上限，只在无活动任务租约时执行，
并以新 runtime instance receipt 证明完成。它不安装/启用扩展、不扩大 host permission、不恢复
登录、不绕验证码，也不打开或聚焦 Chrome 页面。

### 36. 当前最终 Done-Gate 到底过了吗？

答：没有。三日期 Round 17 已真实走完异步 job/polling、checkpoint 和 3600 秒预算：HTTP job
`succeeded/complete`、47/47 模型调用成功，但业务 runner 为 `done_gate_failed`。中间 pair 的
Evidence Arbiter policy 冲突修复后，同日期 focused run 也完成，仍因住宿比价 1/2 而主控阻塞；
去哪儿是 `bounded_provider_pending`；同程单路 canary 曾是 `login_required`，用户已明确跳过该住宿
来源。它不再是待登录事项，但跳过也绝不算第二个价格，因此当前结论仍是未通过。
只有新的 sealed bundle 退出码 0 且 `done_gate.passed=true` 才能改口。2026-08-03 的成功文件必须
称为历史 v3/canary。

危险回答：不要说“旧版通过过一次，所以现在也算通过”，也不要把 HTTP job `succeeded` 等同于
业务决策 `ACCEPT`。

## 现场演示顺序

1. 先打开 `/api/v1/agents/runtime`，说明当前 model provider、required 状态、记忆/RAG 和独立运行边界；
2. 提交一组回放或可控 live request，展示 Query Strategist 与 Search Supervisor 的 proposal、
   applied waves 和拒绝理由；
3. 展示报价 stable identity、缓存/single-flight、capture skew 与 candidate generation audit；
4. 注入 fixture 事件，展示单 Event Diagnoser→确定性 Repair→主 Verifier→异构 ReVerifier→事件
   安全门；再用 global fixture 展示共享 96 ledger、`C=256/raw=18` 预检和 fan-out 前 `HUMAN_BLOCK`；
5. 打开日期 benchmark 的负结果和 fair single-vs-multi 平局，证明没有挑数字；
6. 最后并排展示 2026-08-03 历史 v3/canary、Round 17 的
   `job=succeeded + done_gate_failed` 和 policy 修复后的 focused `HUMAN_BLOCK`，主动读出为何
   HTTP/模型成功不能覆盖真实价格证据不足。

## 面试前证据索引

- 模型、上下文、记忆、RAG：`docs/model-context-memory-rag.md`
- 全量红队问题：`docs/interview-red-team.md`
- 声明边界：`docs/claim-ledger.md`
- 日期负结果：`docs/date-search-benchmark.md`、`docs/date-search-hybrid-v2.md`
- single-vs-multi 公平合同：`docs/benchmark-agent-architectures.md`
- 真实 Chrome 门：`docs/done-gate.md`
- 当前 strict 失败证据包：`benchmarks/results/live-done-gate-v4-round17-async-v13.json`
- 历史 v3/canary 证据包：`benchmarks/results/live-flight-only-final-done-gate-2026-08-03.json`
- 当前三日期失败包：`benchmarks/results/live-done-gate-v4-round14-strict-v13.json`
