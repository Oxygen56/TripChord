# TripChord（旅弦）

[![CI](https://github.com/Oxygen56/TripChord/actions/workflows/ci.yml/badge.svg)](https://github.com/Oxygen56/TripChord/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![React](https://img.shields.io/badge/React-19-61DAFB)](https://react.dev/)
[![License](https://img.shields.io/badge/License-MIT-green.svg)](LICENSE)

## 产品是什么

TripChord 面向自由行规划：用户给出灵活的出发/返程日期窗口、结构化同行人数和偏好，系统枚举合法日期，在成功覆盖的已连接来源中，输出**一份**满足硬约束、且完整人民币同行总价最低的最终行程。

这里的“最低价”有明确边界：只在成功覆盖、总价可比、税费和产品身份足够明确且报价仍新鲜的已连接来源之间比较；不承诺全网最低、余位锁定、最终成交价或自动下单。局部恢复、动态重规划是配套能力，不是产品主命题。

TripChord 是独立项目，不依赖 OxygenTeam、Codex、ChatGPT 或任何特定 AI 编程工具。历史马尔代夫运行可能由外部开发编排监督，但那不是 TripChord 的产品运行时。

## 当前状态：把证据说清楚

| 能力 | 当前可以准确声称的事实 |
|---|---|
| 需求与人数 | 结构和校验支持成人、儿童、婴儿、房间及儿童年龄；现有 OTA 适配器遇到混合同行人会明确拒绝而非静默错算，不能宣称已真实支持所有人数组合 |
| 日期搜索 | 在当前最多 400 个日期对的支持边界内，确定性枚举合法日期全集；不是为每个日期创建一个 LLM |
| 来源查询 | 携程、去哪儿等通过本机 Chrome Companion 的只读实验路径；覆盖、登录、验证码和页面结构仍可能阻断 |
| 价格比较 | 归一化完整 CNY party total 后，仅比较成功覆盖且证据足够新鲜的来源 |
| 模型 Agent | 已接线并有本地及有限真实模型路径证据；不等于整套真实 OTA 规划已经通过验收 |
| 回放与测试 | 本地回放、单元测试和集成测试可重复；它们不是实时平台采用证据 |
| 内部性能 | 66 个日期组合的确定性集成路径已全量执行：858 个逻辑查询合并为 648 个唯一采集，去掉 210 个跨日期重复查询；3 个固定 worker 与 provider lane 将最后一个查询的内部计划启动时间压到 290 秒，加保守执行/收尾预算为 530 秒 |
| 平台批量能力 | 存在 fail-closed 的 range 契约候选，但当前内部优化不依赖平台提供批量接口；没有生产适配器时不会宣称已具备该能力 |

当前用户界面只发布一份 final plan；内部可以保留候选和排序诊断，但不会向用户发布多份“推荐”。

马尔代夫材料是有限的本地证据：其中可核对一次需求理解和 Candidate Curator 的模型调用，其余主要步骤由确定性代码执行。它不能证明多模型已经真实协作、长期记忆改变了结果，或系统已经生产采用。

## 多 Agent 如何分工

运行时把模型的不确定性和事实计算分开：

1. **需求理解**：把自然语言转成日期窗口、party、目的地、硬约束和可撤销偏好；缺失或冲突字段先阻断或请求澄清。
2. **完整日期枚举**：确定性代码生成固定的合法日期全集，并做日期/晚数换算。不会按日期动态复制 LLM。
3. **固定有界 worker/provider lane**：交通、住宿和接驳 source workers 按预声明的 provider、权限、并发和超时执行；它们是非 LLM workers，不为凑 Agent 数量包装成自治 Agent。
4. **证据与报价归一**：确定性解析来源回执、产品身份、币种、税费、party total、新鲜度和失败状态。
5. **候选策展**：汇总可比报价并形成有限候选池；内部可有排序诊断，但最终只选择一份计划。
6. **硬校验**：确定性检查日期、人数、房间、航段、住宿衔接、预算、地点和来源证据，模型不能覆盖这些门。
7. **独立风险审查与修复建议**：模型可以指出软风险、提出替换或补查建议；修复执行、复验和发布仍由确定性代码控制。
8. **安全发布与偏好记忆**：重验通过后才发布 final plan；只有显式稳定偏好才可形成记忆候选。

完整日期探索阶段只运行固定有界的 source lanes 和确定性归一化。只有在最终选中的日期上，才进入需要模型角色的候选评议、风险审查、修复建议和解释阶段；这避免把“日期数”误写成“LLM Agent 数”。详见[架构说明](docs/architecture.md)。

### 偏好记忆边界

显式确认的稳定偏好可以撤销、过期，也可以被本次请求覆盖；本次请求中的明确选择优先于历史偏好。实时报价、库存、余位、班次和可订状态永不写入长期记忆，下一次必须重新查询或复验。记忆/RAG 只帮助理解偏好和非实时能力，不把历史价格当当前事实。

## 本地运行与外部依赖

没有模型或平台密钥时可以运行回放；配置兼容 OpenAI API 的模型服务后可启用模型 Agent；用户在本机 Chrome 中主动授权并登录平台后，才可实验只读实时核价。TripChord 不下单、不支付、不使用优惠券、不绕过验证码或平台风控。

```bash
uv sync --locked --all-groups
npm ci
uv run alembic upgrade head
uv run uvicorn tripchord.main:app --app-dir apps/api/src --reload
```

另开终端运行 `npm run dev`，访问 `http://localhost:5173`。默认报价是明确标注的回放数据。真实平台实验还需要本机 `Chrome Companion`、用户授予的域名权限、有效登录态、网络和仍兼容当前网页的 DOM 合同，见 [Chrome Companion](apps/browser-companion/README.md) 与[平台能力矩阵](docs/providers.md)。

模型默认关闭：`MODEL_PROVIDER=none`。启用外部模型时，模型只接收预算化需求、结构化证据摘要和角色允许的工具回执；不会接收 Cookie、登录凭据、Chrome profile 或 bridge secret。外部供应商的数据留存和地域政策仍由用户选择的 endpoint 决定。

## 证据和声明边界

- 回放、fixture、单元测试、benchmark 和 Agent 自述只能证明相应的本地行为，不能替代真实平台运行或采用证据。
- “已接线”“本地测试验证”“真实平台实验”“目标态”是四个不同声明层级，不能混用。
- 当税费、party total、产品身份、新鲜度或必要来源覆盖不足时，系统应返回待确认/阻断，而不是伪造最低价。
- 66 个日期组合的 530 秒是内部调度、去重和伪平台集成证据，不是真实 OTA 网络在十分钟内完成的证明。平台批量接口只是可选增强，不是本轮性能成立的前提。

## 相关文档

- [多 Agent 架构](docs/architecture.md)
- [平台能力矩阵](docs/providers.md)
- [模型、上下文、记忆与 RAG](docs/model-context-memory-rag.md)
- [声明与证据边界](docs/claim-ledger.md)
- [持久化记忆](docs/persistent-memory.md)
- [AGENTS.md](AGENTS.md)
