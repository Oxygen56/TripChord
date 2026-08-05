# 8 月完整日期宇宙精查预算基准

## 它回答什么

本冻结 v1 基准一般化地取“2026 年 8 月出发、住 5–8 晚”，共有
`31 × 4 = 124` 个日期组合。它不是本项目用户原话“玩 5–8 天”的精确窗口：正式需求
合同按 `住宿晚数 = 日历旅行天数 - 1` 换算，因此该案例应为 4–7 晚，仍是 124 对。
TripChord
可以在本地廉价枚举 124 个组合，但不能把“枚举日期”偷换成“对每个组合、每个平台都进行
实时精查”。后者会放大页面限流、登录风控、报价捕获时间差和浏览器租约成本。

本基准冻结 32 个 **test synthetic universe**（4 种噪声/缺失/精查失败条件 × 8 个
未与 calibration 重叠的 seed），每个 universe 都包含完整 124 对真值。对最大精查预算
3、5、8，比较：

- `adaptive`：当时的默认、现已降级为实验注入项的 `AdaptiveDateRefiner`；每轮只能利用
  粗价、覆盖率及已经付费精查的观测；
- `coarse_cheapest`：只精查粗价最便宜的前 K 个；
- `fixed_stratified`：按时间顺序等距分层，每层固定取中点；
- `chronological_first_k`：固定取最早的 K 个日期组合。

指标为 Oracle Top-3 的 `Recall@3`、所查可推荐方案相对全宇宙最低可推荐方案的价格
regret、没有找到任何可推荐方案的失败率、实际精查覆盖率及成功真值覆盖率。
regret 只在至少查到一个可推荐方案的 scenario 上取均值；失败 scenario 单独计入失败率，
避免用虚构价格填补，但解读 regret 时也必须同时展示失败率，防止幸存者偏差。

## 无标签泄漏合同

候选视图使用 `AuditableDatePair`，不含 `exact_total_cents`。策略只拿到一个单次
`query(pair_id)` 接口；每次选定日期后才能看到该日期的精查结果。完整 exact oracle
必须在 selection 显式关闭之后才能读取，并且只用于计算指标。测试还会改写所有未选择
日期的 oracle 值，验证任何策略的已选序列不发生变化。

输入文件和每个 scenario 都有 SHA-256；静默修改真值会使基准失败。生成器、冻结输入、
评测结果分别位于：

- `benchmarks/generate_date_search_scenarios.py`
- `benchmarks/scenarios/date-search-full-universe-v1.jsonl`
- `benchmarks/evaluate_date_search.py`
- `benchmarks/results/date-search-full-universe-v1.json`

## 冻结结果与不舒服的结论

聚合 test 结果如下；regret 单位为分，越低越好。

| 精查预算 | 策略 | Recall@3 | 平均 regret | 失败率 | 精查覆盖率 |
|---:|---|---:|---:|---:|---:|
| 3 | adaptive | 0.115 | 46,727 | 6.2% | 2.42% |
| 3 | coarse_cheapest | 0.240 | 36,124 | 3.1% | 2.42% |
| 5 | adaptive | 0.229 | 33,062 | 0.0% | 4.03% |
| 5 | coarse_cheapest | 0.323 | 22,532 | 0.0% | 4.03% |
| 8 | adaptive | 0.302 | 20,267 | 0.0% | 6.45% |
| 8 | coarse_cheapest | 0.406 | 12,880 | 0.0% | 6.45% |

该 adaptive 在总体上明显输给简单的粗价前 K，不能包装成“自适应搜索已经优于基线”。
它只在 `high-noise-sparse-prior` 条件的预算 5/8 上同时取得更高 Recall 和更低 regret，
说明探索项可能在粗先验极差时有价值，但不足以证明应该全局启用。synthetic calibration
和 test 都不授权据此修改真实 OTA 策略；下一步应先冻结真实授权回放集，再校准“何时利用、
何时探索”的门控策略，并用新的未见 test 集复核。

## 可以怎样回答面试官

TripChord 不实时穷举 124 对，也不声称 3–8 次精查找到了全月最低价。系统先廉价枚举完整
日期宇宙，用可审计粗先验分配有限精查预算，并展示覆盖率和 regret 的离线边界。更重要的
是，冻结基准推翻了“复杂 adaptive 必然更好”的初始设想：live 默认已改为对 Query
Strategist 校验后顺序执行 bounded Top-K，adaptive 只保留为显式注入实验项。若模型
不可用，则沿用确定性粗排顺序。这个切换遵循当前唯一总体不劣的冻结基线，但仍不等于
证明 Top-K 在真实 OTA 上更优。

运行：

```bash
uv run python -m benchmarks.evaluate_date_search \
  --output benchmarks/results/date-search-full-universe-v1.json
uv run pytest benchmarks/tests/test_date_search_benchmark.py
```
