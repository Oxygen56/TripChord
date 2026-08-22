# 长任务 Runtime 选型实验

这是一个与生产依赖隔离的恢复对照，固定使用三个保存的马尔代夫日期组合：

- TripChord 当前实现用生产 `DurableLivePlanningJobRepository` 保存第一个结果，显式交还 lease，再由新的 repository 实例继续；这不是进程崩溃实验。
- LangGraph 使用原生 `SqliteSaver`，在第二个节点首次抛错后用同一 `thread_id` 恢复。
- DBOS 2.30.0 使用同一原生 workflow/step 定义与 SQLite system database；workflow 内的 `crash_once` step 首次写 marker 后真实 `os._exit(73)`，第二进程用同一 system DB 和同一 workflow input 由 `launch()` 原生恢复未完成 step，绝不调用 `restart_workflow`/`fork_workflow`。

TripChord、LangGraph 和 DBOS 的这次恢复都没有重复已经完成的第一个日期组合。DBOS 的 `crash_once` step 被重试一次，pair 计数来自跨进程 side-effect JSON 日志，并记录了稳定 workflow id。三者的失败语义并不完全相同，因此这个结果用于识别恢复边界，不被包装成通用性能排名。

LangGraph 和 DBOS 仍没有替代 TripChord 的任务身份、lease、日期组合唯一性和领域重规划。局部修改另由生产 `LocalReplanner` 验证，本实验不使用手工 state dict 冒充旅行重规划。

机器可读结果位于 [`benchmarks/results/durable-runtime-framework-selection.json`](../../benchmarks/results/durable-runtime-framework-selection.json)。

运行器会在写入 JSON 后检查 `all_contracts_passed`；任一已完成 pair 重复、恢复计数不符、workflow id 缺失、DBOS 双进程退出码不符或 workflow 未完成，命令以非零退出。`true` 才表示本次对照合同通过。

## 复现

在仓库根目录运行：

```sh
uv run --project experiments/durable-runtime-selection python3 experiments/durable-runtime-selection/run_experiment.py
```
