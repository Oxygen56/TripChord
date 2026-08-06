# Claude Code：TripChord v0.2 → v1.0 自治实施总提示词

你现在是 TripChord 的主实施工程师、产品工程负责人和最终验收负责人。请直接进入并持续开发：

`/Users/oxygen/Documents/个人项目/tripchord`

你的目标不是再写一份计划，也不是只做几个演示页面，而是严格依据仓库中的
`docs/roadmap.md`，把当前 v0.1 参考实现逐版本推进到可独立安装和体验的 v1.0 产品形态，
并通过机器可判定、证据可回放的最终 Done-Gate。

除非遇到下文明确列出的“只能由用户处理的边界”，不要在每个阶段向我确认，不要因为任务规模、
上下文长度、运行时间或测试较多而停止。你应当自行调查、实现、测试、发现偏差、修复并继续推进。
如果当前上下文即将耗尽，先更新持久化交接文件，再在下一上下文从该文件继续，不能重新从头分析。

## 一、开始前必须完整阅读

在修改代码前，完整阅读而不是只搜索摘要：

1. `AGENTS.md`
2. `README.md`
3. `docs/roadmap.md`，它是本次产品化的最高优先级目标合同
4. `docs/architecture.md`
5. `docs/providers.md`
6. `docs/data-source-policy.md`
7. `docs/done-gate.md`
8. `docs/claim-ledger.md`
9. `docs/model-context-memory-rag.md`
10. `docs/persistent-memory.md`
11. `docs/operations.md`
12. `docs/resume-project.md`
13. `SECURITY.md`、`.github/workflows/ci.yml`、数据库迁移、现有测试与 benchmark

若旧文档与 `docs/roadmap.md` 冲突，不得偷偷选择更容易实现的旧语义。先查代码和证据，保留旧版本
兼容层，在阶段评审中写明迁移方式，并以产品化路线图中已经确认的决定为目标。

## 二、当前已知状态与声明边界

- 当前仓库是完整的独立项目，不依赖 Codex、ChatGPT 或 Claude Code 才能运行；运行时模型接口必须
  保持 OpenAI-compatible、可替换和可关闭。
- 当前工作树可能已有 `README.md` 与 `docs/roadmap.md` 的未提交修改，它们是本次产品化工作的既有
  输入。先执行 `git status --short` 和 `git diff`，保护所有既有修改，不得 reset、checkout、clean、
  stash、rebase、force 或覆盖不属于你的内容。若仍在 `main`，在不丢失这些修改的前提下创建或复用
  本地产品化分支；核对无误后把路线图作为独立基线提交，绝不自动 push。
- 当前代码已有多 Agent、动态预算、Chrome Companion、Planner–Verifier–Repair–ReVerifier、
  事件重规划、回放、后训练和大量测试；优先抽取复用，不要另起一个互不相连的“v2 演示”。
- 当前平台集合、前端类型、Companion 权限和部分 Done-Gate 仍有固定三平台假设；这是 v0.2 的首要
  根因，不是只改 UI 文案即可完成。
- 当前已知真实证据中，模型调用成功、HTTP job 成功和 Source 全部进入类型化终态，都不等于业务
  Done-Gate 通过。既有 Round 17 的业务门仍失败，住宿精确可比报价不足。开始时必须重新核对仓库
  当前证据；没有新的真实证据就不得改写这一结论。
- `confirmed_empty`、`bounded_provider_pending`、`bounded_no_exact_quote`、`login_required`、
  `captcha_required`、`dom_drift`、`timed_out` 都不能被包装成第二个价格。
- 当前用户已明确跳过同程海外酒店；除非用户在你的当前会话中作出新的显式决定，不得重试、恢复或
  计入覆盖。智行当前只有候选能力审计，没有稳定可审计的 PC 报价结果面，不得进入 Planner。
- 旧 v0.1 strict Done-Gate 的历史失败证据必须保持可回放。新路线图允许单来源有限方案，因此应新增
  版本化 product-v1 gate，不能削弱旧 strict gate、重命名旧终态或改写历史证据来制造成功。

## 三、你已获得和没有获得的权限

你可以自主执行：

- 修改本仓库内的代码、测试、迁移、文档、fixture、benchmark、CI 和本地构建配置；
- 安装锁文件已经声明的依赖，运行本地服务、回放、测试、静态检查、迁移和容器构建；
- 创建本地分支和本地阶段提交，但不得把既有用户修改混入无关提交；
- 使用不访问真实 OTA、不产生外部副作用的本地 fixture、干净浏览器 E2E 和模型桩；
- 在不改变产品边界的前提下做必要架构决策，并记录 ADR 与权衡。

你没有自动获得以下权限；不得把其他会话对 Codex 的授权视为对你的授权：

- 访问用户当前 Chrome 登录态或真实 OTA 平台；
- 扩大 Chrome host permissions、安装/启用扩展、恢复登录或处理验证码；
- 发起付费模型调用、使用未在环境变量中明确提供的 API Key；
- 下单、支付、使用/领取优惠券、读取订单、Cookie、密码或支付信息；
- 调用未公开私有接口、逆向绕过平台页面能力、风控或访问控制；
- 推送 GitHub、创建 PR、发布 Release、发布 Chrome Web Store 扩展或 affiliate 链接；
- 删除用户数据、执行破坏性 Git 操作或不可逆数据库迁移。

只有在确实需要上述权限时才询问用户，而且一次只提出一个具体请求。等待授权期间继续完成所有
不受阻塞的本地工程、回放、fixture、文档和安全测试，不得因为一个平台登录问题停下整个项目。

## 四、不可改变的产品合同

### 1. 本地优先

API、Web、数据库、Browser Companion、平台登录态和用户自己的 LLM Key 默认留在用户机器上。
首版不做托管用户浏览器、账号和支付状态的云端 OTA。可选云能力不能成为本地核心闭环的依赖。
Browser Bridge 必须继续只绑定 loopback，使用强随机且分权的 token、严格 CORS 与脱敏日志；不得把
bridge/control/lease token 写入 Git、模型上下文、旅行数据或公开证据。

### 2. 默认查询全部“当前合格”的平台，而不是全部已知平台

平台选择粒度是 `provider × vertical`。默认集合必须严格等于：

`已认证 ∩ 支持当前垂类 ∩ 用户已授权 ∩ 当前无已知阻断 ∩ 未冷却 ∩ 用户未关闭`

健康未知但没有已知阻断时可进入搜索；运行中发现登录、验证码或 DOM 漂移时，以相应终态结束。
用户关闭的 scope 不得产生浏览器任务、模型工具调用、重试、刷新、failover 或事件重规划访问。
任何 Agent 都不能自动重新开启。搜索启动时冻结不可变 Selection Snapshot 和 SHA。
Browser Companion 必须保持最小权限；不得为了实现方便增加 `cookies`、`debugger`、全站通配 host
permission 或能够读取账号/支付状态的高权限。新增官方域名必须经过独立 capability/profile 版本和
用户逐项授权。

### 3. 等全部已选来源终态后再规划

Planner 必须严格晚于本次所有已选 Source 的终态。等待期间 UI 只展示平台/垂类进度和原因，不展示
部分价格、临时预算或候选方案。终态不等于报价成功；失败、登录、验证码、页面漂移、超时和取消
也必须类型化落地。不得无限等待，不得让 LLM 修改 deadline，不得把失败伪装成成功来通过调度器。

只有一家来源有可用报价时允许输出有限方案，但必须明确“未形成跨平台比价”。零报价时只能输出
阻断报告和安全重试动作，预算与方案必须为空。

### 4. 允许跨平台混合组合

机票、每段酒店、接驳和活动可来自不同平台。不得隐藏设置“同平台优先”，不得假设组合优惠，
除非存在同条件、可回链的明确证据。比较必须绑定人数、房间、日期、稳定商品身份、权益、税费、
行李、早餐、退改和币种，不能把不同商品口径混为同价。

### 5. 只提供官方预订交接，不提供支付能力

产品文案统一为“官方预订跳转”“去官方页面”。TripChord 不下单、不支付、不用优惠券、不锁库存。
每个组件必须先完成同平台、同条件实时重核价，再经过 URL 安全门，才生成短期 handoff。价格、条款、
身份或库存发生实质变化时旧 handoff 立即失效，并重新进入 Verifier，必要时 Repair。

现有 `TravelOffer.booking_url` 和 `BrowserQuote.page_url` 都不能直接暴露为跳转。必须新增独立的
`OfficialHandoff` / `OfficialDetailLocator` / receipt 合同，逐跳校验 scheme、官方 host、path、
query keys、redirect 和报价绑定。拒绝短链、开放重定向、账号、登录、订单、checkout、cashier、
payment、coupon 等路径。平台不能稳定定位商品时，退化为官方搜索页加精确参数卡片，不猜 URL。

### 6. 已预订只来自用户声明，并成为强约束

打开官方页面不等于已预订。只有用户显式点击“我已完成预订”才能生成 append-only 的本地
Booking Fact；不得读取平台订单来自动确认。Booking Checklist、Booking Fact、
Protected Component Constraint 和解除保护请求必须分层建模并持久化。

Candidate Generator、Optimizer、Planner、Verifier、Repair、ReVerifier、Safety Gate 和事件重规划
都必须消费同一组已预订约束。未解除保护时，已预订组件不得被删除、换项、改日期、改人数或静默
重定价。事件命中已预订项时，输出冲突和用户处理选项，不自动替换。

### 7. Agent 与硬代码边界

- Agent 负责：需求语义、偏好权重建议、查询优先级、候选评议、风险批判、修复提案和解释；
- 确定性代码负责：权限、Selection Snapshot、任务白名单、并发/预算、终态、超时、金额、时间、
  稳定身份、URL、硬约束、Booking Fact、发布和最终安全否决；
- 不要为了“更像 Agent 项目”把金额、权限、URL 或真值校验交给 LLM；
- 也不要把 Agent 退化成无作用的装饰调用。模型提案必须在允许范围内真实影响查询顺序、候选评议、
  Repair 或解释，并留下 proposal、applied action 与拒绝原因；
- Agent 数和模型并发继续由确定性预算控制器按 D/C/G/事件范围动态派生，不能由模型无限扩容，
  当前请求级逻辑 Agent 上限 96、模型 HTTP 并发上限 12、Chrome lease 上限 6、去哪儿住宿并发
  上限 1；若要调整，必须先有版本化 benchmark 和反例证据，不能因为模型便宜就放宽。

## 五、持续实施协议

### 1. 建立可恢复的实施账本

创建并持续更新 `docs/productization-progress.md`，至少记录：

- 当前版本、当前阶段和最后一个完成的最小任务；
- 当前 Git commit/branch、既有修改与本轮修改；
- 已完成的领域合同、迁移、API、UI、Companion、测试和 benchmark；
- 精确运行过的命令、结果、失败原因和证据文件；
- 尚未完成项、外部阻塞、下一条可直接执行的命令；
- 当前可对外声明与绝对不能声明的内容。

对路线图每条工作项和退出门使用“未开始、实现中、代码完成、已验证、外部阻塞”之一，并链接到
对应代码、测试、运行产物和提交 SHA。“代码完成”不能自动升级成“已验证”。

每个版本创建 `docs/phase-reviews/product-vX.Y.md`，结论只能是：通过、有条件通过、返工、调整计划。
每次上下文切换前先更新实施账本；下一上下文先读账本、`git status` 和最近 diff 后继续。

### 2. 先建立基线

在业务代码修改前：

1. 检查 Git 状态和当前差异，标记既有用户修改；
2. 检查 Python、uv、Node、npm、数据库和浏览器测试环境；
3. 安装锁定依赖；
4. 运行当前静态检查、完整测试、Web 构建、迁移检查、benchmark 和 Companion release gate；
5. 将基线失败与后续引入失败分开记录；
6. 搜索所有固定三平台、`exactly three`、`3/3`、固定 provider union、固定超时和旧 `booking_url`
   的代码落点，形成迁移清单。

不要为了得到绿色基线删除失败测试、放宽断言、改黄金结果或关闭严格模式。

### 3. 实施原则

- 按 v0.2 → v0.3 → … → v1.0 顺序推进，不能先做漂亮的预订 UI 再补平台真值和终态屏障；
- 每次先写/更新领域合同与反例测试，再迁移实现，再接 API/UI，再跑端到端；
- 复用现有实现，先抽取测试缝隙，避免一次性重写 10,000 行 live system 或 2,000 行前端主页面；
- 数据库变更必须有前向迁移、兼容读取、失败恢复和必要的回滚验证；
- API、事件和持久化结构必须版本化，迟到的旧 generation 结果不得污染新运行；
- 修复真实 planning failure 时新增冻结 benchmark 或 mutation case；
- 测试只证明测试覆盖范围，不能自动升级成真实平台能力；
- 不允许留下只在 fixture 中存在、未接入生产路径的“完成”功能；
- 不允许用 TODO、假数据、吞异常或自动 fallback 隐藏未完成项。

### 4. 每个版本的固定检查点

每完成一个版本：

1. 执行该版本定向测试与全部回归；
2. 执行 `git diff --check`、Ruff、Mypy、Python tests、Web build/tests、迁移和 Companion 合同；
3. 运行对应 replay、故障注入、mutation 和浏览器 fixture E2E；
4. 对照 `docs/roadmap.md` 逐项列出计划、实际、偏差、证据和未验证边界；
5. 更新 README、architecture、providers、operations、claim ledger 和 phase review；
6. 只在退出门真的满足时标记该版本通过；
7. 创建范围清晰的本地阶段提交，然后立即进入下一版本，不等待用户例行确认。

可使用子 Agent 并行完成只读审查、后端/前端/Companion 独立测试、政策调查和红队，但共享文件写入
由主控串行整合或放在隔离 worktree。主控必须亲自审查 diff 并重跑证据门，不能因子 Agent 声称完成
就勾选退出门。

若测试失败，先定位根因并修复；不得仅报告失败就停止。若遇到外部阻塞，先完成其余全部可执行项，
为阻塞点准备一条可重复的恢复命令和预期证据，再向用户请求最小权限或动作。

## 六、分版本实施与退出门

### v0.2：动态平台内核

必须完成：

- 新增稳定的 `ProviderScopeKey(provider, vertical)`、`ProviderCapability`、用户选择、Eligibility 与
  不可变 Selection Snapshot（含 capability version、adapter version、SHA、排除原因）；
- 先形成内部统一的 `ProviderAdapter` 接口和版本化 capability profile，再用 Registry 替代后端、
  前端、Companion、Query Planner、live system 与 Done-Gate 中固定
  三平台集合；保留显式旧 profile 兼容，不静默篡改历史证据；
- Companion 使用逐 scope 可选 host permission，心跳报告实际授权 scopes、adapter/contract version
  和 runtime instance，不再要求三平台全部在线；
- API 与 UI 提供 `平台 × 垂类` 能力矩阵，默认全选合格单元格，用户可逐项关闭；
- 每个必需垂类至少保留一个合格来源，为 0 时在任何模型/浏览器调用前拒绝启动；
- 拆分必要的 Provider Policy、Source Runtime、Barrier、Planning Pipeline 和前端页面边界。
- 从 v0.2 起，每次修改 Companion 后立即运行其定向合同和 release gate；v0.9 负责把同一门正式
  接入远端 CI，而不是此前都不验证。

退出门：0、1、2、3、4 平台回放都能构建正确 DAG；关闭/未授权 scope 的浏览器任务、模型工具调用
和网络访问均为 0；伪造 provider/snapshot hash 原子拒绝；旧三平台场景兼容测试不回退。

### v0.3：全来源终态屏障与诚实发布

必须完成：

- 统一跨垂类 Source 终态；
- 实现持久化 SearchRun、SourceAttempt、TerminalReceipt 和 Completion Barrier；
- 调度器新增原生 `ALL_TERMINAL` 依赖与独立 settle node，Normalizer 依赖 settle，Planner 再依赖
  Normalizer，禁止靠 `success=true` 伪装失败来源；
- 新增 `ScopeCancellationTombstone` 与 generation；普通重试、publication refresh、failover、延迟
  唤醒和 event replan 都必须检查，迟到结果不得进入 Planner；
- deadline/总预算由确定性 timeout profile、波次和所选矩阵派生；到期主动物化 `timed_out`；
- SSE 在屏障前只发送进度/终态，屏障后才一次性给最终结果；
- 持久化恢复、取消、幂等和重启语义明确。

退出门：Planner 首次调用时间严格晚于最后一个已选 Source 的 `terminal_at`；无 queued/running/unknown
仍可发布的路径；取消后新增任务为 0；单来源如实披露；零报价无预算和方案；无无限等待。

### v0.4：跨平台最终方案与覆盖解释

必须完成：

- Planner 可为机票、每段酒店、接驳和活动独立选择 provider，并保留 package/component provenance；
- 建立稳定商品/权益比较身份和完整金额口径；
- Verifier 校验分开交易、衔接、行李直挂、退改、税费、币种和不可同时满足的条件；
- 方案 UI 优先展示有证据的省钱、稳妥和少折腾取舍；证据不足时不凑满三个方案；
- 展示每组件平台、报价时间、到期时间、可比条件、跳转次数、覆盖来源和失败终态；
- 把 Source settlement、exact quote coverage 和 comparable component coverage 分开统计。

退出门：固定回放能生成 A 平台机票 + B 平台酒店；返回顺序不影响确定性结果；金额/权益属性测试
无错配；Agent 不能绕过 Hard Verifier；Repair 后独立 ReVerifier 重新计算。

### v0.5：官方预订跳转与逐组件清单

必须完成：

- 新增 `OfficialHandoff`、`OfficialDetailLocator`、短期/单次 receipt、URL Policy 和失效机制；
- 每个平台声明详情页、预填搜索页或仅参数卡片能力；
- 组件级重核价只查询同 provider × component，不默认重跑整趟行程；
- 用户动作分两步：“重核价并查看差异”与“去官方页面”，后台不能自动打开/聚焦；
- 价格、权益、身份、日期、人数/房间或库存变化时撤销旧 handoff，重新验证/Repair；
- 逐跳拒绝非官方域名、短链、开放重定向、登录、订单、checkout、payment、coupon 和未知路径；
- 构建逐组件清单和建议操作顺序，但此阶段点击绝不能产生 booked 状态。

退出门：每个 handoff 可回链同一 plan version/component/offer/query/revalidation receipt；所有危险
URL mutation 零错误放行；旧/过期 receipt 不可用；没有稳定 deep-link 时安全降级；自动化测试绝不
提交订单或支付。

### v0.6：已预订保护与事件闭环

必须完成：

- 持久化 Booking Checklist、Booking Item、User Booking Acknowledgement、append-only Booking Fact、
  Protected Constraint 和 Constraint Override Request；
- 只有明确用户动作能创建/撤销 Booking Fact；打开链接、Agent 输出和平台页面文字都不能；
- protected invariant 贯穿候选生成、优化、Planner、Verifier、Repair、ReVerifier、Safety Gate、
  local/global/event replan；
- 实现手动事件、用户开启的监控、预订前重核价变化和公共事件；
- Impact Analyzer 先确定受影响集合，只重查必要 scope；
- 已预订项受影响时进入用户处理状态，未明确解除前不自动替换；
- 输出旧/新版本 diff、保留项、变化原因、证据和预算。

退出门：属性/故障测试中未解除保护的已预订组件修改率为 0；局部修复保留无关组件；全局重规划仍
继承保护；解除保护必须显式确认并留痕；实时变化只能来自新工具回执，不能来自 RAG/旧缓存。

### v0.7：Provider SDK 与认证门

必须完成：

- 把 v0.2 已接入生产路径的内部 ProviderAdapter 接口提炼成公开 SDK，并提供 capability profile
  schema、fixture 模板和 conformance test kit；v0.7 不是第一次发明 adapter 抽象；
- 实现开发、影子、测试、合格、冷却、停用状态机；只有 `certified_active` 可默认全选；
- 认证按 `provider × vertical` 独立进行，机票成功不能证明酒店；
- selector/URL/permission/profile 变更都产生新版本和 SHA；
- 影子 adapter 不进入 Planner、覆盖分母或默认选择；
- 对智行、同程酒店或其他候选先做 fixture/影子能力，只有获得当前会话真实只读授权并通过 canary、
  URL 安全和报价绑定门后才能升级为合格；不能为了凑数量绕过平台边界。

退出门：新增 provider 只需 adapter + profile，不改核心 Planner/Barrier 枚举；可按垂类一键冷却；
历史 run 保留旧 profile；未认证来源访问数和报价贡献为 0。若缺真实授权，完成全部 SDK/影子工程并
把该来源保持非活跃，继续后续版本，不得把外部阻塞冒充代码未完成或认证成功。

### v0.8：本地优先的完整产品体验

必须完成：

- 本地启动器/安装器统一管理 API、Web、数据库迁移和 Bridge；
- LLM Key 使用系统安全存储或等价安全方案，不进入仓库、前端持久化、旅行数据库、模型上下文和日志；
- 首次设置向导覆盖本地服务、模型 smoke、Companion 配对、逐平台权限与登录健康；
- 首页改为旅行工作流，拆分平台设置、搜索进度、方案比较、预订清单、事件中心和证据详情；
- 默认折叠 Agent DAG、token 和内部回执，普通用户优先看到发生了什么、为什么、下一步；
- 任务/清单跨刷新和重启恢复；提供数据导出、删除、日志脱敏和默认关闭的匿名遥测；
- 完成键盘、屏幕阅读器、非颜色状态、可读字号和 WCAG 2.2 AA 审查；
- 为签名扩展/安装包准备可复现产物，但发布到商店或公网必须另行获得用户授权。

退出门：全新机器按公开说明可完成 replay；真实模式逐项展示权限；秘密不进入日志/遥测；升级、迁移、
备份和失败回滚有测试；Chrome 安装需要用户确认，但开发者重载不成为公众日常待办。

### v0.9：公开测试版可靠性与评测

必须完成：

- 把 Companion JS 合同、Python control tests、build-meta/release seal 和 host permissions 加入远端 CI；
- 增加动态选择矩阵、ALL_TERMINAL、慢源、取消竞态、迟到结果、URL/offer binding、Prompt Injection、
  崩溃恢复、幂等和 booked invariant 的冻结 benchmark/mutation suite；
- 使用干净 Chrome + 本地 fixture 做浏览器 E2E，普通 CI 不依赖真实账号；
- job/monitor/清单迁入可恢复存储，验证重启和多租户隔离；
- 增加 secret scan、CodeQL/SAST、SBOM、依赖审计、构建 provenance 和固定第三方 Actions 版本；
- 提供本地可观测面板，分开记录终态、报价覆盖、Barrier 延迟、模型成本、Repair 和 handoff；
- 真实 OTA canary 只能在获得用户授权的发布门运行，证据脱敏并绑定 commit/profile/runtime/query SHA。

退出门：Python、Web、Companion、迁移、benchmark、浏览器 E2E、安全和可复现构建全部进入 CI；
未授权访问、提前发布、危险链接、错误 booked 和已预订静默变化均为 0；性能门基于冻结真实基线，
不用拍脑袋数字。

### v1.0：最终产品与 Done-Gate

实现机器可执行的最终产品门，例如 `scripts/run_product_done_gate.py`，原子输出脱敏的
`benchmarks/results/product-v1-done-gate.json`。证据包至少记录 commit、capability/profile/adapter/
Companion 版本、模型模式、选择快照、每个 Source 终态、Barrier 时间、报价覆盖、最终裁决、handoff、
booked invariant、所有子门和证据引用。

最终门至少分层验证：

1. fresh clone/锁文件/迁移/wheel/Web/容器/安装器可复现；
2. replay 模式完整走通动态选择 → 全终态 → 跨平台方案 → handoff → 用户确认 → 带保护重规划；
3. 干净 Chrome + 本地恶意 fixture 验证权限、配对、后台、URL 和 Prompt Injection；
4. OpenAI-compatible 模型在有环境变量授权时完成 required-model smoke 和结构化 Agent 链；
5. 每个对外宣称合格的真实 `provider × vertical` 都有未过期、已授权的只读 canary；
6. 全平台真实 E2E 只在所有外部条件已满足时运行；未满足则准确指出哪个外部门未过，不能伪造。

Done-Gate 只有在所有**适用于当前发布声明**的门通过时，才允许 `passed=true` 且进程退出 0。
HTTP job 成功、测试成功、模型调用成功或所有 Source 终态都不能单独触发通过。某个真实平台认证失败时，
可以从 `available/default-selected` 中移除后发布核心产品，但 claim ledger 必须明确不再宣称该能力。

## 七、最低验证命令

根据仓库当前实际脚本调整，但不得少于以下范围：

```bash
git status --short
git diff --check

uv sync --locked --all-groups
uv run ruff check .
uv run mypy apps/api/src
uv run pytest

npm ci
npm run build
npm test
npm audit --omit=dev --audit-level=high

uv run python scripts/browser_companion_release_gate.py

uv run python -m training.train_sft --validate-only
uv run python -m training.train_dpo --validate-only
uv run python -m training.policy_reranker

uv run python benchmarks/evaluate.py
uv run python benchmarks/evaluate_planning.py
uv run python benchmarks/evaluate_repair.py
uv run python benchmarks/evaluate_events.py

docker compose config
docker compose build
```

数据库迁移必须使用临时数据库验证 `upgrade head`、`alembic check` 和必要的兼容/回滚路径。
Companion 源码变化后，只能在确认 diff 合理时显式运行 release gate 的 build-meta 更新模式，再重新执行
只读 gate；同时按合同更新扩展版本。不得手工编辑 build-meta 或 release seal，也不能为了过 gate
随意刷新封板。新增产品门、浏览器 E2E 和安全 benchmark 后，把它们加入本清单与 CI。

```bash
uv run python scripts/browser_companion_release_gate.py --update-build-meta
uv run python scripts/browser_companion_release_gate.py
```

最终全门还要运行与 CI 等价的 Python/npm 依赖审计、secret scan、SBOM 与构建来源校验；不得把发现的
高危问题静默加入 allowlist。

最终至少保留五个反表面端到端验收：动态 0/1/2/4 provider；慢源或迟到结果绝不提前触发 Planner；
用户关闭 scope 后浏览器任务、模型工具调用和网络访问全为 0；开放重定向/支付路径 handoff 全拒绝；
任意事件序列下未解除保护的 booked component 修改率为 0。

## 八、绝对禁止的伪完成方式

- 只更新 README、路线图、接口类型或静态页面，没有把真实生产路径接通；
- 新增一套旁路 demo，旧 live pipeline 仍固定三平台；
- 用更多 Agent、更多提示词或更多模型调用代替权限、金额、终态、URL、Booking Fact 等硬合同；
- 让 Agent 自动恢复用户关闭的平台、登录、验证码或 host permissions；
- 把 pending、empty、login、timeout、DOM drift 变成报价或第二平台覆盖；
- 在全部已选来源终态前展示部分方案；
- 把 `page_url`、模型生成 URL 或 HTTP 200 当成安全官方详情页；
- 点击链接后自动标记 booked，或在重规划中静默替换已预订组件；
- 从 RAG、缓存或旧截图恢复“当前实时价格”；
- 更新 golden/benchmark 只为让失败消失，不解释行为变化；
- 通过删除测试、放宽 Hard Verifier、关闭 required-model 或降级严格模式来变绿；
- 把本地 fixture、单平台 canary、Source 完成或模型成功写成全平台真实业务通过；
- 在没有用户明确授权时 push、发布、访问真实平台或使用付费服务。

## 九、只允许因这些事项暂停并请求用户

仅当剩余工作真的依赖以下条件且所有其他本地工作已完成时，才可暂停：

1. 用户需要亲自授予或扩大某个平台域名权限；
2. 用户需要亲自登录、处理验证码或恢复平台账号状态；
3. 需要真实 OTA、付费 LLM、签名证书、Chrome Web Store、affiliate/partner 资格；
4. 需要公开 push/release、不可逆数据迁移或删除用户数据；
5. 外部平台政策存在无法由代码或公开资料消除的实质性产品取舍。

暂停时不要泛泛说“需要你操作”。必须给出：已完成内容、精确阻塞门、为何不能替代、最小用户动作、
完成后将执行的命令、预期成功证据。一次只问一个问题。

## 十、最终交付报告

持续工作直到最终 Done-Gate 真实通过，或只剩上述用户专属外部阻塞。最终报告必须先给一句话结论，
然后提供：

- v0.2 至 v1.0 每个版本的状态和证据文件；
- 用户实际能完成的完整流程；
- Agent 真正做了哪些决策，哪些仍由确定性代码裁决；
- Python/Web/Companion/迁移/benchmark/安全/安装器的精确验证结果；
- replay、fixture、模型、单平台真实 canary 和全平台真实 E2E 分层结论；
- 当前支持和默认选择的 `provider × vertical`；
- 当前仍不能声称的能力；
- Git 状态、本地提交和所有未公开外部动作；
- 若仍有外部阻塞，只列用户必须处理的最小动作，不能把普通工程问题推给用户。

现在开始：先进入仓库，阅读全部合同，检查并保护现有修改，建立基线和
`docs/productization-progress.md`，然后直接实施 v0.2。不要只回复计划。
