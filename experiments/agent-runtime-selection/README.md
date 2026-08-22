# Agent Runtime 选型实验

这是一个与生产依赖隔离的离线对照。TripChord 当前实现、PydanticAI、LangGraph、OpenAI Agents SDK 和 Google ADK 分别处理同三个马尔代夫历史场景，并走各自原生的模型与工具循环。

## 最新结果

五种 Runtime × 三个场景共 15 个单元全部通过。每个单元都按同一顺序调用：

```text
inspect_requirements → inspect_candidate → verify_candidate
```

随后生成同一个 `Proposal` 结构，并通过框架外的确定性结果检查。当前实现真实调用生产 `ModelToolAgent + ScriptedModelClient + ToolRegistry`；另外四组使用各自的 Agent/Graph/Runner 和本地 scripted model。

本次锁定并实际运行的版本为：PydanticAI 1.107.5、LangGraph 1.2.11、OpenAI Agents SDK 0.22.0、Google ADK 1.39.0。机器可读结果位于 [`benchmarks/results/agent-runtime-framework-selection.json`](../../benchmarks/results/agent-runtime-framework-selection.json)。

每个单元都保留一次预热和三次测量结果；任何预热或测量失败都会进入 JSON，使 `all_contracts_passed` 为 `false`，并令运行器以非零状态退出。生产模块只在 custom 单元内部加载，随后从模块缓存清理，避免污染后续适配器。

这个实验只证明五种方案都能承载相同的工具闭环。它没有调用真实模型或 OTA，不证明答案质量、生产稳定性、当前价格和库存，也不能单独选出优胜框架。记录的本地耗时不进入选型。

## 复现

```bash
uv lock --project experiments/agent-runtime-selection
uv sync --project experiments/agent-runtime-selection
uv run --project experiments/agent-runtime-selection \
  python experiments/agent-runtime-selection/run.py \
  --output benchmarks/results/agent-runtime-framework-selection.json
```
