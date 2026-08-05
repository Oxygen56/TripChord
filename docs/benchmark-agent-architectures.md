# 单 LLM Agent 与多 Agent 公平基准

## 这个基准解决什么问题

旧的 `benchmarks/evaluate_agents.py` 中，`single` 是“只保留一个候选再跑确定性
优化”的代理基线，不是真正的单 LLM Agent。因此旧结果可以验证回放回归，但不能用来
声称“多 Agent 比单 Agent 更强”。

`benchmarks/evaluate_agent_architectures.py` 新增了真正的架构 A/B 框架：

- 单 Agent 组由同一个 LLM Agent 负责初始规划和事件后的自我修复；
- 多 Agent 组拆成 Planner、Verifier、Repair、Orchestrator；
- 两组使用同一模型标识、同一冻结任务集、同一组只读工具、相同温度，以及完全相同的
  模型调用、工具调用、总 token 和单次输出预算；
- 运行顺序按任务交替采用 A→B、B→A，减轻实时试跑中固定先后顺序的偏差；
- 候选能否发布由两组共享的确定性硬约束检查器裁决，LLM 不能覆盖它。

冻结套件是 `benchmarks/scenarios/agent-architecture-v1.jsonl`，包含预算、最大中转、
必须早餐、禁止过夜、证据过期，以及售罄、涨价、报价过期、路线变化、早餐权益丢失等
12 类任务，其中 5 类必须在事件后修复。

## 完全相同的工具合同

两个架构都只能看到下面三个工具，结果中会保存工具合同 SHA-256：

1. `inspect_requirements`：读取已确认硬约束；
2. `inspect_candidates`：只读取当前库存版本和原始候选字段，不返回预计算答案；
3. `verify_candidate`：按候选 ID 运行同一套硬约束检查。

Agent 必须至少调用一次工具后才能提交结构化提案。未知工具、未知候选、无效 JSON、
工具循环或超预算都会显式失败，不会被包装成成功。

## 指标定义

| 指标 | 含义 |
| --- | --- |
| `valid_plan_rate` | 最终既选择硬约束有效候选、又建议接受的任务比例 |
| `valid_candidate_found_rate` | 不考虑 Agent 最终措辞，是否找到硬约束有效候选 |
| `proposed_hard_constraint_violation_count` | LLM 曾建议接受无效候选的次数；不会被安全门掩盖 |
| `released_hard_constraint_violation_count` | 用独立审计实现重新检查发布候选后的违规数 |
| `repair_success_rate` | 事件确实让原候选失效的任务中，是否找到并接受有效替代 |
| `mean_latency_seconds` | 每任务端到端墙钟延迟 |
| `mean_model_calls` / `mean_tool_calls` | 每任务实际模型调用和工具调用 |
| `mean_total_tokens` | 每任务实际输入加输出 token |
| `total_estimated_cost_usd` | 按显式价格卡计算的总估算成本 |
| `budget_breach_count` | 因公平预算耗尽而失败的任务数 |

同时报告“提案违规”和“发布违规”很重要。发布指标不能复用
`accepted = model_accept and hard_valid` 后再计算 `accepted and not hard_valid`，否则它会在
逻辑上恒为 0。基准先执行可替换的发布门，再由另一套仅依赖原始候选字段的审计函数
重新计算违规；测试还会注入一个故意损坏的发布门，确认该指标确实能变为非零。

## 离线可复现回放

```bash
uv run python -m benchmarks.evaluate_agent_architectures \
  --output benchmarks/results/agent-architecture-v1.json
```

默认模型是 `scripted:shared-policy-fixture-v1`。它不能读取候选的预计算 `violations`；
必须先读取原始约束和候选，再主动调用 `verify_candidate`。它对两组执行相同冻结策略，
用途只有：

- 验证 A/B 调度和指标实现；
- 验证同模型、同工具、同预算门确实生效；
- 回归事件注入、Repair 和硬约束发布门；
- 测量框架自身的调用、token 与延迟开销。

它不是真实 LLM，也不能证明哪种架构质量更好。输出因此固定包含：

```json
{"evidence_tier":"scripted_harness_validation","winner_claim_allowed":false}
```

## 可选真实模型 pilot

真实模型调用必须显式确认成本、显式限制样本量，并通过环境变量提供密钥：

```bash
export TRIPCHORD_MODEL_API_KEY='...'
uv run python -m benchmarks.evaluate_agent_architectures \
  --mode live \
  --provider anthropic \
  --model '<明确可用的模型 ID>' \
  --limit 5 \
  --ack-live-cost \
  --input-usd-per-million '<输入价格>' \
  --output-usd-per-million '<输出价格>' \
  --output benchmarks/results/agent-architecture-live-pilot.json
```

OpenAI 兼容服务使用 `--provider openai_compatible --base-url ...`。若没有
`--ack-live-cost`、模型名、Provider 或显式 `--limit`，命令会拒绝启动。

真实模型 pilot 仍然只是在冻结合成任务上比较两个架构。它可以回答“在这批任务、这个
模型和这组预算下发生了什么”，不能外推为生产 OTA 效果、线上 SLA 或“多 Agent 普遍
优于单 Agent”。更强的结论至少需要：预注册样本量与统计检验、多个随机种子、至少两个
模型家族、未见真实任务人工盲评，以及真实供应商链路的独立证据。

## 面试时的准确说法

可以说：

> 我发现早期 75% 的 single 指标其实是单候选确定性代理，不是单 LLM Agent，不能支持
> 架构优越性结论。后来我重做了同模型、同工具、同任务、同总预算的 A/B harness，分别
> 暴露模型提案违规和安全门发布违规，并支持付费真实模型小样本试跑。离线 scripted
> 结果只证明 harness 与安全边界可复现，不把它包装成真实模型优势。

面试官追问“为什么多 Agent 调用更多还算公平”时，回答：公平指两组拥有相同的预算上限
和能力集合，不是强制它们消耗相同资源。实际调用数、token、成本和延迟正是被比较的
因变量；若多 Agent 因角色拆分耗尽同一预算，它会按失败计入，而不是偷偷扩大预算。
