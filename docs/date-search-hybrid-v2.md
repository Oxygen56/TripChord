# Guarded Hybrid 日期精查实验：停止结论

## 为什么做第二轮

冻结 v1 synthetic 基准发现，当前 `AdaptiveDateRefiner` 总体输给简单粗价 Top-K。为避免
只停留在负面描述，第二轮尝试了一个仅使用可观测字段的 guarded hybrid：

1. 所有预算先查询粗价排名前三，形成确定性低价保底；
2. 精查预算少于 5 时不探索，因为三次观测后已经没有足够探索空间；
3. 只有完整 124 对候选的平均平台覆盖率不高于阈值时，剩余预算才交给 adaptive；
4. 策略永远只能通过 `QueryOnlyOracle.query(pair_id)` 看到已经主动精查的结果。

阈值只使用单独文件 `date-search-calibration-v1.jsonl` 搜索。入围规则预先规定为：预算
3/5/8 的 Recall 不降、平均 regret 不升、失败率不升。最终冻结参数为覆盖率阈值
`0.40`、最小探索预算 `5`、粗价保底 `3`，policy manifest 内部哈希为
`ec2e7c8494cb935f36a85a31de3b8dba8df8bf3b4a1365430124d9a8cf1835f7`。

## 天数和晚数合同

旧 v1 基准是一般化 5–8 晚，不等同于本项目用户原话“玩 5–8 天”。正式合同为：

```text
住宿晚数 = 日历旅行天数 - 1
玩 5–8 天 => 住 4–7 晚
```

因此参数冻结后才生成的新 sealed holdout 使用 4–7 晚、31 个出发日，共 124 对；含
4 种条件 × 16 个由 policy hash 派生且与旧 seed 不重叠的 universe。输入 SHA-256 为
`15e16c9887da24a199425da1a0b8a271a46d2da0f22cfb5eebf9140e80e1f39f`。

## 一次评估结果

| 预算 | Guarded Hybrid Recall@3 / regret | 粗价 Top-K Recall@3 / regret |
|---:|---:|---:|
| 3 | 0.2240 / 30,750 分 | 0.2240 / 30,750 分 |
| 5 | 0.3021 / 21,136 分 | 0.3125 / 18,090 分 |
| 8 | 0.3698 / 14,370 分 | 0.3750 / 15,038 分 |

预算 8 的 regret 改善约 4.4%，但 Recall 略降；预算 5 同时出现 Recall 下降和 regret
上升。它没有满足“所有预算不退化，预算 5/8 均有 material 改善”的预冻结门，因此：

- `accepted_as_planning_candidate=false`
- `live_default_change_allowed=false`
- 不把 **guarded hybrid** 移入 planning 层，也不因该实验改用 hybrid。

这是停止结论，不是待包装的成功指标。它说明仅凭 synthetic coverage proxy 做策略门控
仍不稳定；下一次尝试必须先获得真实授权回放的粗价误差、缺失和精查失败分布，重新创建
calibration 与从未查看的新 holdout。不能继续反复查看这份 holdout 后调阈值。

另一个独立的保守决策是：旧 adaptive 在 v1 总体基准已经明显输给 Top-K，因此 live
默认改为 `Query Strategist 重排 → 硬预算校验 → bounded Top-K 顺序精查`，而不是继续
把证据不支持的 adaptive 当默认。Query Agent 的选择仍真实改变后续查询顺序；模型不可用
时才回落到确定性粗排。`AdaptiveDateRefiner` 仍可显式注入做实验。这个默认切换依据的是
“当前唯一总体不劣的冻结 baseline”，不是 guarded hybrid 过门，更不是声称 Top-K 在
真实 OTA 上已经证明更优。

## 证据污染披露

原 v1 test 在提出 guarded hybrid 任务之前已经被查看，因此第二轮结果把它标记为
`contaminated regression only`，不能称为盲测或 held-out。真正的接受/停止判定只来自
参数冻结后生成的 4–7 晚 sealed holdout。
