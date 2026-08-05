# TripChord Done-Gate

> 当前结论（2026-08-05）：**最新 strict required-model Done-Gate 尚未通过。**
> 三日期 Round 17 已真实走完异步 job/polling、日期对 checkpoint 与 3600 秒服务端预算：job 为
> `succeeded/complete`，47/47 次 DeepSeek 调用成功，三个 checkpoint 依次为
> completed / failed / completed；runner 仍正确输出 `run_status=done_gate_failed`。证据包：
> `benchmarks/results/live-done-gate-v4-round17-async-v13.json`。

> 中间日期对失败的原因是 Evidence Arbiter 的 schema/Repair 泛化规则与公共船费“只披露、不计入
> 确认小计”专用规则冲突。修复后，同一 2026-08-21 至 2026-08-26 日期对真实聚焦复测完成，
> 23/23 次模型调用成功且不再发生 `RuntimeError`。业务仍保持 `HUMAN_BLOCK`：携程是唯一精确住宿
> 价格来源，去哪儿为 `bounded_provider_pending`；单路同程住宿 canary 则返回
> `login_required`。用户已在 2026-08-05 明确跳过同程海外酒店，后续不再查询或等待登录，
> 也不把它计入覆盖率。因此代码/Agent 缺陷已修复，但双平台住宿真实价格证据仍不足 2 家。

> Companion `0.1.16` 随后通过 release seal 在后台自动重载，未打开或聚焦 Chrome；同日期聚焦
> canary 获得携程 18 条精确含税住宿报价，去哪儿则被官方登录页重定向并返回
> `login_required`。派生摘要为
> `benchmarks/results/live-browser-lodging-focused-v16-2026-08-05.json`；逐报价、
> 去哪儿导航链、后台 reload 回执、查询/build/runtime 绑定与 input/result SHA 已封存在权限
> `0600` 的
> `benchmarks/results/live-browser-lodging-focused-v16-2026-08-05.sealed.json`，可用
> `scripts/capture_live_browser_evidence.py --verify` 离线复算。
> 该结果仍是 1/2，不触发 Publication Gate，也不把登录门改写成无房或价格。

本文件把“代码存在”“离线测试通过”“单日期真实观察”和“当前 strict Done-Gate 通过”拆开。
只有退出码为 0 且证据包中的 `done_gate.passed=true`，才允许说当前版本通过最终验收。

## 当前判定

- 离线 Agent / 规划 / 后训练 / 全栈工程门：已有独立冻结证据，按各自声明边界报告。
- 当前 strict 代码门：已实现本地只读 Browser Companion、每日期对 13 路浏览器搜索 +
  4 路 iCom 官方公共接驳搜索的 17 源 DAG、报价归一化、灵活日期抽样、
  Planner–Verifier–Repair、主控裁决、事件局部重查、API 与 UI。
- 当前 strict 真实只读证据门：**未通过**。focused run 的
  `source_execution_completeness.complete=true` 只说明 13 路浏览器 Source 都有类型化终态；
  `exact_quote_comparison_coverage.complete=false` 才是发布阻塞，因为所选住宿分段只有 1/2 个
  不同 provider 提供 `quote_found`。`confirmed_empty`、有界未命中、平台仍在搜索和账号安全门
  都不能伪装成第二个价格。
- 2026-08-04 live-v4 Round 17：异步控制面完成、三个 pair checkpoint 和 job-bound 模型回执均
  已封存；47/47 模型调用成功。中间 pair 的 required Evidence Arbiter 因专用 policy 冲突失败，
  Flexible 层按日期对隔离后继续第三 pair。runner 因该 pair 未完成、另外两 pair 的住宿覆盖均为
  1/2 而得到 13 个级联失败检查；synthetic 事件按合同跳过，未伪造推荐。
- Round 17 policy 修复后聚焦复测：同一中间 pair 的 normalization、Evidence Arbiter 与 publication
  均成功，23/23 模型调用成功；最终仍因携程 `quote_found`、去哪儿
  `bounded_provider_pending` 而 `HUMAN_BLOCK`。单路同程 Maafushi 住宿 canary 为
  `login_required`、0 报价；用户随后明确跳过该住宿来源。它不再构成待登录事项，也不会被重试或
  计作第二个 provider price。当前发布阻塞仍是可比住宿报价覆盖只有 1/2。
- Companion `0.1.16` 聚焦核价：受限后台 supervisor 已用 seal + build SHA + runtime instance
  receipt 自动升级扩展；携程返回 18 条精确含税住宿报价，最低已审计展示价为 CNY 778/晚；
  去哪儿精确查询被 `user.qunar.com/passport/login.jsp` 官方登录页截断并结构化为
  `login_required`。sealed artifact 保留了 18 条逐报价的 provider、脱敏 page URL、时间、
  evidence SHA、金额、计价单位、税费和酒店身份，以及去哪儿有界重定向链、未聚焦隔离窗口、
  applied reload lineage 和源码产物 hash；原始页面长文本、凭据、Cookie 与 tracking 值均未落盘。
  release binding 复核当次 Companion、build-meta、release seal 与当前固定源码均为同一 SHA。
  这是新的外部登录阻塞证据，不是解析器价格失败，也不改变 1/2 判定。
- 2026-08-03 v3/canary 历史证据：旧能力矩阵的一次 Chrome 只读运行形成两个推荐日期，并完成
  一次注入涨价后的单平台局部重查。证据见
  `benchmarks/results/live-flight-only-final-done-gate-2026-08-03.json`。它早于当前 required-model、
  冻结住宿候选、两精确报价平台和 receipt-v2 合同，只能证明当时版本，不能抵扣当前 gate。
- 2026-07-31 live-v4 round 3：携程真实只读 canary 已完成去程选择、导航恢复和有界候选
  重规划，但最终页面仍只有 `starting_price_only`，未输出精确往返报价；飞猪与去哪儿
  住宿并发 canary 当时均结构化返回 `login_required`，详见
  `benchmarks/results/live-v4-canary/round3-readonly-flight-and-login-gate.md`。
- 2026-07-31 live-v4 round 4：携程真实只读 canary 在去程页有界扫描 12 条含价候选后，
  即使返程页仍未形成精确组合，也能保留并由后端复核
  `comparison_price_only` 签名收据；系统继续输出 0 条精确报价，比较价不能进入
  Planner、预算或最终方案。项目全量 Python 332 项、浏览器 DOM 夹具 105 项、
  Live-v4 专项 57 项、Web 10 项及扩展四组合同测试均通过。该增量解决证据丢失，
  不改变三平台生产 Done-Gate 尚未通过的结论，详见
  `benchmarks/results/live-v4-canary/round4-ctrip-comparison-receipt.md`。
- 2026-07-31 live-v4 readiness round 5：报价最大年龄 15 分钟与最少 2 个可推荐方案已
  冻结进场景，命令行不得降级；运行器对预检、灵活日期执行、方案选择、事件重查和
  Done-Gate 求值的失败均原子落盘，并保护已有成功证据不被后续失败覆盖。全量 Python
  335 项、Ruff、Mypy、Web 与扩展合同回归通过。该增量保证失败可复现，不替代仍待完成的
  三平台生产证据，详见
  `benchmarks/results/live-v4-readiness/round5-terminal-evidence-and-frozen-thresholds.md`。
- 2026-07-31 live-v4 round 6：三平台 Companion 心跳正常；飞猪、去哪儿精确住宿
  canary 连续第三个目标回合仍结构化返回 `login_required`。这是当时 Edge 登录态的历史
  阻塞，不是当前操作要求，详见
  `benchmarks/results/live-v4-canary/round6-auth-block.md`。

### Companion `0.1.16` 自动重载边界

用户仍须首次安装 unpacked extension、配对 loopback bridge，并对具体 provider 域名授予 host
permission。之后 Agent 可在后台重载已安装的 Companion，但只能使用枚举 reason，不能传路径、
URL、hash 或脚本。目标必须通过 source SHA、manifest/content runtime、build metadata 与当前用户
所有的 `0600` release seal 精确核验；命令绑定当前 runtime instance、幂等键、TTL、冷却和重试
上限，且不能与活动浏览器任务租约并存。成功必须由新 runtime instance 的 receipt 证明；协议不
打开或聚焦 Chrome 页面。

该能力不能安装/启用扩展、扩大域名权限、恢复登录、处理账号安全门、绕过验证码或加载未封板
源码。没有 fresh control companion 或任何 identity 不一致时失败关闭。

## 当前 strict 强制验收合同

| 验收项 | 通过门槛 |
|---|---|
| 用户需求 | 2026 年 8 月出发、玩 5–8 天（确定性换算为 4–7 晚）、杭州到马累、2 成人、1 房可被结构化且不覆盖用户明示值 |
| 日期探索 | 最多 3 个可审计日期对；缺少完整三平台价格日历时标记“抽样、未穷举” |
| Source worker | 当前每日期对精确 17 源：携程 6 路、去哪儿 6 路、同程机票 1 路，加 4 路 iCom 官方公共接驳；这些是固定单工具 worker，不包装成模型推理 Agent |
| iCom 任务覆盖 | `public_transfer_task_ids` 精确为 continuous-outbound、split-outbound、split-inbound、continuous-inbound 四路；coverage 必须 4/4，且每路至少一个可转换可用班次 |
| iCom 查询合同 | 四路 `IComTransferSearchResult` 分别匹配首日/次日出岛、倒数第二日/末日回机场的精确日期与方向，均为 2 成人 |
| iCom 官方证据 | 三个 source URL 必须落在 `sfs-api.icomtours.com` 精确公共 GET allowlist；每个关键字段带 value SHA、response SHA 和新鲜 `captured_at` |
| Source 执行完整性 | 严格模式按能力矩阵计算：携程 6/6、去哪儿 6/6、同程机票 1/1；每路须达到允许的类型化终态。禁用的同程酒店不得派发或计为成功 |
| 住宿精确比价覆盖 | 选中住宿方案的每个分段至少 2 个不同 provider 为 `quote_found`；`confirmed_empty`、`bounded_no_exact_quote` 与 `bounded_provider_pending` 只完成证据分类，不计作价格 provider |
| 住宿库存四态 | `QUOTE_FOUND`（命中精确报价）；`CONFIRMED_EMPTY`（receipt-v2 同查询、同 tab/window/runtime lineage 的两次 parser-v1 观测，至少间隔 2 秒并分别校验 canonical SHA）；`BOUNDED_NO_EXACT_QUOTE`（在冻结扫描上限内未命中，不是空库存证明）；`BOUNDED_PROVIDER_PENDING`（平台仍在实时搜索，不是空库存证明） |
| 真实并发 | Scheduler 首波按当前 Source DAG 并发；浏览器任务的 `claimed_at → updated_at` 区间另行证明三个启用机票来源存在实际重叠 |
| 页面证据 | 非 fixture/replay parser；可见查询被触发并确认；报价在新鲜度窗口内 |
| 完整往返组合 | 机票必须使用 `tripchord-visible-dom-v3`；四个出发/到达时刻均非空且带显式时区；仅 `round_trip_complete + round_trip + final_for_combination` 可进入归一化，去程预览一律失败关闭 |
| 平台流程证据 | 携程/同程必须是 `staged_outbound_return` 且证明执行过精确“选为去程”；去哪儿必须是 `combined_roundtrip_card` 且不得执行去程选择 |
| 浏览器动作链 | 初始方案与事件后最终方案的原始机票证据均须回溯到结构化 `action_trace`；动作只允许搜索、筛选、选为去程、重选去程，出现预订、下单、支付、优惠券或账号动作立即失败 |
| 人数库存边界 | 携程/去哪儿/同程的最终组合均须明确绑定请求中的 2 成人；比较价或人数未确认报价不能成为最终已接受机票 |
| 报价归一化 | 同人数、日期、房间、币种、含税口径；未知字段不得默认成已确认 |
| Verifier | `INITIAL` 交接单必须逐项匹配 Planner 选中候选的 ID、版本、组件与违规项；硬错误必须原样成为 Repair 拒绝理由，不能被静默改写或绕过 |
| Repair / ReVerifier | `reverify-travel-package` 是初始 DAG 强制节点；Repair 候选与组件差异必须经独立 `REVERIFICATION` 复验，候选 ID、版本和组件完全一致且无硬错误后才可接受 |
| 主控裁决 | 只能消费 Planner → Verifier → Repair → ReVerifier 的完整 `planning_handoff`，旧单体执行结果或任一空交接单必须失败；裁决只能是“接受 / 拒绝并重新规划 / 阻塞并转人工”之一 |
| 事件注入 | 涨价或售罄后仅重查受影响平台和精确日期段；替换 1 个组件后必须生成 `event_handoff`，且 `EVENT_REVERIFICATION` 的候选 ID、版本、组件与 Repair 输出完全一致 |
| 预算与证据 | CNY 已确认小计按整数分逐项相等；若入选 iCom `published_base_fare`，必须另列 supplemental USD、明确 non-all-in、税费未知、未换汇且未计入 CNY 小计 |
| 只读安全 | 浏览器 Source worker 只能用 `browser_bridge_search`，iCom Source worker 只能用 `icom_public_transfer_search`；初始与事件 DAG 均不得出现下单、支付、优惠券或账号修改能力 |
| 声明边界 | 不得写“全月最低、全网最低、库存锁定、保证可订” |

机器判定器位于：

- `apps/api/src/tripchord/agents/live_done_gate.py`
- `benchmarks/run_live_done_gate.py`
- `benchmarks/scenarios/live-hgh-mle-aug-2026.json`

## 历史 v3/canary 命令

以下命令和证据只用于复现 2026-08-03 的历史合同，不是当前 strict 正式命令：

```bash
uv run python benchmarks/run_live_done_gate.py \
  --request benchmarks/scenarios/live-hgh-mle-aug-2026-flight-only-canary.json \
  --output benchmarks/results/live-flight-only-final-done-gate-2026-08-03.json \
  --bridge-token "$TRIPCHORD_BROWSER_BRIDGE_TOKEN"
```

脚本会自动完成：

1. 先在 5 秒超时内查询本机、令牌保护的 Companion 状态；必须存在 45 秒内有心跳且同时
   声明携程、去哪儿、同程的 Companion，否则在提交耗时搜索前失败；
2. 灵活日期候选、多平台精确搜索与四路 iCom 官方公共接驳搜索；
3. 核验每日期对 11 路浏览器 Source 与 iCom 4/4、精确日期/方向/人数、官方 URL、字段哈希与新鲜度；
4. 选择第一个严格覆盖的方案，回溯初始与事件最终机票的完整往返组合及只读动作链，并核验
   Planner → Verifier → Repair → ReVerifier → 主控的逐级交接单和拒绝理由链；
5. 对其中最长住宿注入涨价事件；
6. 调用单平台、单日期段重查；
7. 校验事件 Repair → `EVENT_REVERIFICATION` 交接、组件差异、保留率、
   CNY 已确认小计和 supplemental USD 价格边界；
8. 保存完整输入、Companion 预检状态、初次运行、事件运行、检查明细与 SHA-256，schema 为
   `tripchord-live-evidence-v3`。

心跳状态只在 API 进程内存中保存 `companion_id`、平台集合和 `last_seen`，不记录
Cookie、Chrome profile、账号信息或标签页 URL。

退出码 `0` 且 JSON 中 `done_gate.passed=true` 才算通过。退出码 `2` 表示功能运行完成但至少一项
证据门未过；HTTP、登录、验证码或 DOM 问题会直接失败，并保留真实阻塞。

## 当前 frozen-stay strict 门

当前 v4 证据 schema 不覆盖或改写历史 v3/canary 成功包。它修正“搜索前把马累固定解释为
Maafushi”的产品偏差，在任何平台结果返回前冻结三个可审计方案：

- `maafushi_icom`：Maafushi 连住，双向 iCom 官方公开快船；
- `maafushi_split_hulhumale`：Hulhumalé 首末晚、Maafushi 中段，作为脆弱直达方案的
  预注册 Repair 候选；
- `hulhumale_continuous`：Hulhumalé 连住，要求双向、含税、Planner 可消费的机场接驳合同。

冻结集合携带 canonical SHA-256、来源、精确地点、住宿分段、接驳合同和每平台扫描上限。
当前能力矩阵下，每个日期对为 13 路浏览器 Source：携程与去哪儿各含 `hulhumale-full`
精确住宿查询；同程仍只有机票；四路 iCom 独立执行。

住宿库存只允许四个互斥终态：

1. `quote_found`：命中可归一化的精确报价；
2. `confirmed_empty`：v2 双观测密封，同一精确查询、同一 tab/window/runtime lineage，两个
   parser-v1 观测至少间隔 2 秒，并分别绑定时间戳和 canonical SHA；
3. `bounded_no_exact_quote`：只说明在冻结扫描上限内没有精确报价；
4. `bounded_provider_pending`：只说明平台仍显示实时搜索中。

只有第一态贡献精确比价 provider。后二者以及 `confirmed_empty` 均不得升级成可比价格；
`confirmed_empty` 也只能证明这次受审计查询窗口的空结果，不代表平台全量或未来无库存。普通
DOM 漂移、登录、超时、导航失败、旧 `capture_code`、单次观测或扁平诊断字段不能冒充任一更强
库存结论。Bridge、normalizer 和 Done-Gate 会独立重算 receipt-v2；查询、时间间隔、tab/window、
runtime lineage 或任一 SHA 不一致即 fail-closed。选中方案的每个分段至少须有场景冻结的两家住宿平台
`quote_found`，启用的住宿平台仍须全部达到四态之一并保留各自证据。每个
`quote_found` 还必须唯一回链可用 normalization result、原始 snapshot 与可见证据
SHA，并重新核对 provider、分段、地点、日期、人数、房间数和新鲜度。随后由
Planner → Verifier → Repair → ReVerifier → 主控的第二条强交接链绑定同一候选集 SHA、
方案 ID、整包 ID、版本和组件。

三个日期对不会同时把 39 个浏览器任务的独立截止时间压入只有 6 个全局只读浏览器槽位。
当前实现采用日期对批次准入：每批 13 个浏览器 Source 可在 6 个槽位内并发，前一批落定后
下一批才开始自己的等待预算；场景冻结单任务 120 秒、总执行预算 3600 秒。正式 runner 通过
异步 `POST 202 → tenant-scoped GET polling` 等待终态，并记录单调 revision、阶段进度和日期对
checkpoint；这解决长 HTTP 请求先超时的问题。Round 17 已在真实三日期作业中证明该异步
transport 与 checkpoint 链可以完成；该结果只关闭旧的
长 HTTP 超时问题，不代表业务 Done-Gate，后者仍为 `done_gate_failed`。
Done-Gate 还会主动注入一次明确标注的 synthetic `sold_out` 假设故障，事件 source 固定为
`tripchord-synthetic-done-gate-fault-injection`。这不是平台售罄信号、平台售罄证据或自然涨价
证据。离线严格闭环测试已覆盖“排除原商品 → Repair 删除 1/新增 1 → Event ReVerifier →
独立审计 → 主控”；这仍不是平台售罄观测。live runner 只有在已存在严格可推荐方案后，才会对
`affected_provider` 和精确住宿分段发起一次只读重查并尝试封板。运行器要求
`resolve_offer_event` 以无歧义稳定商品身份排除原目标，找到同 provider 的不同可用商品，
Repair 精确删除 1 个并新增 1 个，再经过 `EVENT_REVERIFICATION`、异构独立审计和主控接受。
重查只能生成一个 Source task 和一个住宿 snapshot；其地点、日期、人数与房间数必须与初始
目标组件相同，并保持原冻结住宿方案及精确地点。任何同商品同价刷新、身份歧义、无替代、
全局重规划或人工阻塞都不能冒充这条动态局部重规划证据。

机器判定器与场景位于：

- `apps/api/src/tripchord/agents/live_done_gate_v4.py`
- `benchmarks/run_live_done_gate_v4.py`
- `benchmarks/scenarios/live-hgh-mle-aug-2026-v4.json`

真实执行：

```bash
uv run python benchmarks/run_live_done_gate_v4.py \
  --request benchmarks/scenarios/live-hgh-mle-aug-2026-v4.json \
  --output benchmarks/results/live-done-gate-v4.json \
  --bridge-token "$TRIPCHORD_BROWSER_BRIDGE_TOKEN" \
  --require-model-enhancement
```

场景同时冻结 `maximum_quote_age_minutes=15` 与
`minimum_recommendable_options=2`。同名命令行参数仅保留兼容性，必须与场景值完全一致；
尝试把新鲜度放宽或把可推荐方案下限降低，都会在提交浏览器搜索前失败。

运行器对成功、门禁拒绝和前置失败都原子写入 JSON 证据。完整搜索结束但没有可推荐
方案时，仍会以 `selected_initial=null`、`event=null` 求值全部适用门禁，写成
`run_status=done_gate_failed`，并记录 `skipped_reason=no_recommendable_published_option`；
此时绝不会注入 synthetic 事件。只有登录失效、验证码、异步作业失败、事件重规划失败或
Done-Gate 求值异常等无法形成完整门报告的错误才写成
`run_status=failed_before_done_gate`，保留已完成到该阶段的 Companion 预检、
需求解释、pair checkpoint、`FlexibleLiveAgentRun`、缓存句柄、初始方案或事件结果，并标记
精确 `failure.stage`。登录与验证码不会自动重试；用户恢复外部状态后重新执行。若目标路径已经
保存 `done_gate.passed=true` 的证据，后续失败会改写到带 UTC 微秒时间戳的
`*.failed-*.json`，不会覆盖成功包。

退出码 `0` 且 `done_gate.passed=true` 才能声明当前 strict v4 通过。候选集 SHA 漂移、选中方案
任一分段少于两家精确报价、技术失败被冒充空库存、synthetic fault 未走完同平台实时只读
重查与 ReVerifier、替代商品稳定身份未证实、Repair 跨出冻结集合或主控绕过 ReVerifier，
都会失败关闭。

## 既有离线证据边界

冻结 240 条 Agent 场景、CP-SAT 消融、SFT/DPO、LoRA smoke、Open-Meteo 与公共官网 canary
仍可用于证明各自范围内的调度、约束、安全和训练工程能力，但它们均不能升级为当前 strict
三平台库存证据。历史 v3/canary 成功包同样不能覆盖当前 v4 合同；测试通过也不等于真实平台
DOM 契约已通过。
