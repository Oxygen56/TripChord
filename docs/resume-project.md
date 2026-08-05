# 简历项目成稿

## TripChord（旅弦）— 证据驱动的自由行多 Agent 规划与动态恢复系统

技术栈：Python / FastAPI / Pydantic / 自研 Typed Dynamic DAG / OpenAI-compatible Model Gateway /
BM25 RAG / OR-Tools CP-SAT / SQLAlchemy / PostgreSQL / Redis / React 19 / TypeScript / Chrome Extension /
Docker / Nginx / Transformers / TRL / PEFT

- 从 0 设计可独立运行的自由行多 Agent 系统，将需求理解、日期策略、来源调度、证据仲裁、候选
  策展、风险批判、Repair、ReCritic 与主控裁决拆为具有阶段专属 Context、工具 allowlist、结构化
  输出和失败归因的 Agent；它们可共享同一 Router/进程，不能宣称基础设施故障域独立。模型提案
  真实改变日期精查顺序、Source DAG 波次与候选换选，确定性
  Verifier/Safety Gate 保留金额、权限和硬约束发布权。
- 实现“固定硬上限 + 动态 ReAct 日期分片”的 Agent Budget Controller：按日期/候选/证据缺口和
  pipeline 阶段生成可复算 `ScaleDirective`，经 16 个模板白名单与 96 个逻辑模型 Agent hard cap
  分配；自然语言入口将 Requirement Agent 以 `CONTEXT` 角色与后续规划纳入同一 request-wide
  ledger，并用 Flexible scope start 区分规划增量和整个请求累计；ScaleDirective 只计划规划阶段。
  日期每 12 行分片，400 日期集成测试走通
  34 scout → 3 intermediate merger → 1 final merger，每个节点只能调用只读日期工具并选择合法 ID；
  模型请求门从 1–2 起步、成功加一、失败减半，进程级模型 HTTP 并发最多 12，同时保持 Chrome
  lease 6、去哪儿住宿 1 和日期对 1 不变。四类 synthetic 冻结预算回归为
  `8/19/57/143 → 8/19/57/96`、模型 ceiling `2/6/8/12`；明确该基准无模型/浏览器/OTA 调用，
  也不含文本 Requirement admission，不包装成全请求 Agent 数、真实价格覆盖或性能收益。
- 将发布前重核价 fallback 纳入可审计动态预算：首批和每次额外尝试前，按累计尝试数
  重派生 `ScaleDirective + AgentTemplatePlan`，每个 publication attempt 计 8 个基础模型 Agent，
  最多 `exact_pair_budget=8`；结合已审计 Candidate Scout 增量与 request-wide 96 Agent
  ledger 在下一次浏览器/模型调用前 fail-closed，预算不足写入
  `publication_refresh_shortfall` 并结构化 `HUMAN_BLOCK`，无 500 或隐藏超限。本地回归验证
  3 次逐次 refreeze 与第 2 次不足时无新副作用；不声称真实 OTA 已触发 8 次 fallback。
- 实现 Candidate Scout live 决策分片链：确定性 Planner 生成最多 256 个有界候选，`C>32`
  时按 32 分成最多 8 个可并发调度、服务端绑定的只读 Scout，每个 Scout 仅能调用候选检查
  工具并提名本分片 ID；确定性 Collector 收敛为最多 32 个 decision frontier，Evidence
  Arbiter 逐项审核后只有唯一 `candidate_merger` 可以更新 Planner 初案。候选阶段复用全请求
  96 Agent ledger 与成功加一/失败减半并发门，启动前预留后续必需 Agent 名额，并对
  scope/pool/frontier SHA-256、fallback、并发和 Merger admission 留下类型化回执。65 候选
  32/32/1 集成测试验证越权提名拒绝、伪造 hash 拒绝、预算前置拒绝和唯一写入者；
  该证据来自本地结构化模型/fixture，不声称真实 OTA 已触发 `C>32`。2,000 候选只是
  离线 synthetic controller 预算算术，不是 live Planner 池或全网穷举。
- 实现 Search Supervisor：先通过只读工具检查 provider capability、缓存、延迟、硬预算与 Chrome
  lease，再生成搜索 waves；校验后的提案被物化为实际 DAG dependencies，未知 Source、漏必需项、
  超预算或非只读任务整份拒绝。当前每个精确日期对构建 13 路浏览器 + 4 路 iCom tool-bound
  Source worker；浏览器始终由最多 6 个任务并发执行，不把 worker 数
  包装成浏览器并发数。
- 建立 Provider-neutral 模型网关与上下文工程：默认 `MODEL_PROVIDER=none`，required-model 模式
  失败关闭；Query/Planner/Repair 使用 1600/4000/3000 token 的按角色 Context Pack，工具回执与
  历史记忆共享预算。实现 tenant/user/session/trip 隔离、长期偏好确认/撤销和 BM25 词法 RAG，
  强制实时价格/库存只从本轮工具回执进入上下文；用 DeepSeek `deepseek-v4-flash` 真实完成
  固定 3 请求的 JSON/tool-loop smoke；在后续 focused required-model Chrome 运行中，模型阶段和
  全部浏览器 Source 均真实完成，但住宿精确报价平台仅 1/2，最终 `HUMAN_BLOCK`。将“模型参与”“Source
  执行完整”和“方案可发布”分层记账，不夸大为完整 OTA Done-Gate 成功。
- 针对跨平台报价一致性实现 product/offer 两级稳定身份、10 分钟同分区精确报价复用与
  single-flight、默认 20 分钟抓取时差拒绝门；候选生成使用可审计 beam 与默认 256 cap，输出
  raw/prescreen 数量、结构上界、ID hash 和截断状态；模型候选阶段只能看服务端绑定的
  32 候选 Scout scope 或最终 `<=32` decision frontier，不能声称穷举全部原始组合。
- 设计 `Hard Verifier → Risk Critic → Repair Strategist → deterministic Repair Executor →
  heterogeneous ReVerifier → ReCritic → Orchestrator → Safety Gate` 闭环；第二套 ReVerifier 不调用
  主 Verifier/diff 实现，独立重算 13 类金额/父链/diff/硬偏好/住宿/报价/接驳不变量，反例证明
  即使主 Verifier 被 stub 为通过仍能拦截篡改；handoff 绑定候选 ID、版本、组件 diff 与错误码，任何
  Agent 均不能静默覆盖硬拒绝。
  解释 Agent 的事实陈述必须绑定最终组件与 evidence_ref；在 Explanation/Memory 之后增加
  确定性 Publication Gate，required-model 模式下任一必需阶段失败均不会漏出 `ACCEPT`。
  主控的接受提案还必须绑定 Repair/ReVerifier 的实际最终候选 ID，且证据只能来自该候选；
  未知候选或无关 evidence_ref 在 advisory 模式也会被 Safety Gate 拒绝。Memory Curator 的
  trip/user 候选全部停留在待确认区，只有显式用户确认接口才能写入 RAG。
- 实现有界事件重规划：browser local/iCom 路径只允许一个模型 Event Diagnoser，后续 Repair、
  主 Verifier、异构 ReVerifier 与事件安全门均为确定性；只有 global 升级才禁用近期报价复用并
  重跑完整正常模型 pipeline。`replan_after_event` 与 nested global 复用 request-wide 96 ledger，
  在全局浏览器 fan-out 前按 `C=256、E=true、R=false、raw=18` 做最坏容量预检，额度不足返回含
  required/available 的结构化 `HUMAN_BLOCK`；`AgenticRunSummary.combine` 保留两段 stage/request/
  HTTP 与模型并发审计。增加用户显式开启的周期重核价 monitor，每轮只读重查一个组件；当前
  证据仅为代码和本机 structured-model/fixture，不包装成真实 OTA event 已验证、供应商 push 或
  库存锁定。
- 对“8 月全部日期应该怎么搜”做完整宇宙评测：低成本枚举 124 个日期对，将昂贵 OTA 精查限制
  在 1–8 对；冻结 benchmark 发现旧 adaptive 总体输给粗价 Top-K，后续 guarded hybrid 在新的
  4–7 晚 sealed holdout 也未过不退化门，因此 live 默认改为 `Query Strategist 重排 + bounded
  Top-K`，adaptive 仅保留为实验项，并在输出中明确“抽样、未穷举”。
- 建设分层评测与反例门：240 条/12 类固定种子 synthetic suite 中，静默硬约束/明确偏好违规、
  过期事实、未授权 L3 和死循环均为 0；另构建同任务、同工具、同模型标识、同总预算和共同
  最终审计的 single-vs-multi scripted A/B，两组均为 100%，多 Agent 消耗更多调用与 token，
  因而不宣称质量优势，其可证价值限定为最小权限、阶段化失败归因、结构化交接和并发等待重叠。
- 完成 FastAPI + React + Chrome Companion 全栈控制面，展示 Agent proposal/applied action、模型
  调用、Context 裁剪、候选空间与证据链；将长耗时 live 搜索改为 `POST 202 + job_id → GET/SSE
  进度 → DELETE 取消`，支持五态、tenant 隔离、容量/TTL、错误脱敏与 `Idempotency-Key`，取消
  可传播到 browser bridge；strict runner 使用异步轮询、日期对 checkpoint 与冻结 3600 秒预算。
  该队列为进程内有界实现，主动披露重启不恢复而不包装生产 SLA。
- 为 Chrome Companion 设计受限后台自动重载：当前 `0.1.16` 只有 source SHA、manifest/runtime、
  0600 release seal 与当前 runtime instance 全部匹配时才允许一次有界幂等 reload，并验证新实例；
  不打开/聚焦页面，也不能安装扩展、扩大域名权限、恢复登录或绕验证码。
- 将住宿来源结果建模为 `QUOTE_FOUND / CONFIRMED_EMPTY / BOUNDED_NO_EXACT_QUOTE /
  BOUNDED_PROVIDER_PENDING` 四态；`confirmed_empty` 要求同查询、同 tab/window/runtime lineage 的
  receipt-v2 双观测，Bridge/normalizer/Done-Gate 独立复核。Source 全部完成仍不等于可推荐，选中
  分段必须有 2 个不同 provider 的精确报价。
- 三日期 Round 17 已在异步 job/checkpoint/3600 秒预算下真实完成控制面并封存：47/47 模型调用
  成功，checkpoint 为 completed/failed/completed，runner 为 `done_gate_failed`。中间 pair 的
  Evidence Arbiter 专用 policy 与泛化 Repair 规则冲突已修复；相同 2026-08-21 至 2026-08-26
  聚焦复测以 23/23 模型调用成功完成，但携程仍是唯一 `quote_found`，去哪儿为
  `bounded_provider_pending`，最终 `HUMAN_BLOCK`。同程住宿单路 canary 为 `login_required`，
  用户已明确跳过该来源；它不再是待登录事项，也不贡献覆盖率。
  因此简历只写“真实运行、日期对隔离和失败关闭”，不写当前 Done-Gate 已通过。
  2026-08-03 两推荐日期与 75% 局部保留率仅作为历史 v3/canary 证据。

## 可选的训练工程追加项

- 构造城市组隔离的合成编排 SFT/DPO 数据；小规模未见城市组 Base/SFT/SFT+DPO 为
  66.67%/100%/100%，DPO 未证明高于 SFT。另跑通 SmolLM2-135M-Instruct 的 3-step LoRA
  SFT→DPO 与 4 个 adapter reload；只作为离线训练链路证据，不写“提升中文规划质量”或“已接入 live”。

## 面试主线

先讲两个被实验推翻的方案，而不是从架构名词开始：

1. 原以为 CP-SAT 会让 greedy 在硬约束上大量领先，实际 120 条合成基准中二者都 100% 有效，
   CP-SAT 平均效用仅 +0.83%；因此把重点从“算法名字”转向证据、新鲜度和异常恢复。
2. 原以为 adaptive 日期探索必然优于粗价 Top-K，冻结 full-universe 基准和新 sealed holdout 均未
   支持该假设；因此保留负结果并切回有证据的保守默认。

随后解释为什么仍需要多 Agent：不是因为公平 scripted 评测显示更准——两者都是 100%，而且
多 Agent 更贵——而是旅行系统需要把搜索权限、证据冲突、候选取舍、反方批判和返工隔离成独立
失败域，并让 Verifier/Safety Gate 对模型保持否决权。最后展示最新 strict `HUMAN_BLOCK` 与
Round 17 的 `done_gate_failed` 与后续聚焦 `HUMAN_BLOCK`，说明系统没有拿历史 v3/canary 成功、
HTTP job 成功或模型调用成功掩盖当前双平台住宿门禁未过。

## 简历绝对禁区

- 不写“多 Agent 相比单 Agent 从 75% 提升到 100%”。75% 是历史单候选确定性代理消融，
  不是公平 one-shot LLM Agent；
- 不写“默认使用 DeepSeek 完成实时 OTA 闭环”。默认模型是 none；DeepSeek 已真实参与 focused
  required-model 运行，但最终是 `HUMAN_BLOCK`，不能包装成 Done-Gate 成功；
- 不写“Source execution complete 等于多平台报价完整”，也不把 `confirmed_empty` 或 pending
  计作第二个平台价格；
- 不写“向量 RAG”“全月最低”“全网最低”“库存锁定”“自动下单”“真实用户转化提升”或
  “生产 QPS/SLA”；
- 不写“adaptive 优于 Top-K”或“LoRA 提升中文行程质量”。
- 不写“当前事件预算链已被真实 OTA 自然涨价/售罄验证”；2026-08-03 只是旧 v3/canary 的验收器
  注入页面重查，不包含当前共享 ledger、global preflight 与 summary-combine 合同。
- 不写“96 是实验得到的最优 Agent 数”“96 个 Agent 同时请求模型/浏览器”；可写 Candidate
  Scout/Merger 已接入 live 决策代码并通过本地结构化模型/fixture 集成测试，不写“真实
  OTA 已运行 256 候选”、“并行穷举 2,000 个候选”或“穷举全网组合”。

证据明细见 `docs/claim-ledger.md`，压力问答见 `docs/interview-guide.md` 与
`docs/interview-red-team.md`。
