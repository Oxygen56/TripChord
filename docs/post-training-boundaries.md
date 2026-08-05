# 后训练链路与证据边界

审计日期：2026-08-03

## 结论先行

TripChord 当前存在三类容易被统称为“后训练”的东西，但证据等级完全不同：

1. `replan-policy.json` 是 7 维特征的 pairwise logistic 线性重排器，真实接入了 API
   runtime；它不是 LLM，也不是 SFT/DPO 模型。
2. `orchestration-policy.json` 是 8 维特征的多分类线性 oracle imitation 实验，没有接入
   runtime；其 100% 冻结测试准确率不能写成线上编排能力。
3. `training/runs/*` 中有真实的 LoRA 权重文件，但使用的是
   `HuggingFaceTB/SmolLM2-135M-Instruct`，核心证据运行只有 3 个 optimizer steps。
   它们能证明训练、保存、重载链路走通过，不能证明中文旅行规划质量，而且没有被当前
   API 模型网关加载。

因此准确说法是：“项目具备经过实际三步优化和 adapter reload 验证的 SFT/DPO LoRA
训练链路，并有一个线上使用的轻量重规划重排器。”不能说“线上 Agent 已使用后训练大
模型”或“SFT/DPO 已提升生产规划效果”。

## 数据审计与已修复问题

当前数据合同版本为 `post-training-data-v2`。生成器和训练入口现在会拒绝重复 ID、空
数据、跨 split 城市组、跨 split 精确 prompt、无效 JSON、chosen/rejected 同值、两侧
schema 不一致、以及 completion 内的 `rejection`、`label_source`、`oracle_action` 等
答案侧标签标记。

本次修复了四个捷径：

- 行程 DPO 的 rejected JSON 以前独有 `rejection` 字段，模型无需理解行程就能识别负样本；
  现在拒绝原因只保存在训练 metadata，chosen/rejected completion 使用同一 schema。
- compact itinerary 输入以前删除 availability 和 travel time，却要求输出精确时间表；
  现在保留可用窗口和路程矩阵，输入至少包含求解所需事实。
- orchestration prompt 以前直接包含 benchmark `category`；现在删除这个生成器标签，线性
  policy 的特征也只保留 runtime 可观察信号。
- SFT completion 以前自行输出 `verification.verdict=pass`；现在只输出
  `verification_handoff.required=true`，确定性 Verifier 仍是唯一权威。

三个 manifest 均记录 source/file SHA、split 审计和声明边界。当前阻断型问题为 0：
城市组交叉 0、record ID 交叉 0、精确 prompt 交叉 0。

## 仍然存在且不能包装掉的限制

### Split 不是未见任务泛化

训练、验证、测试按“冻结城市组”划分。它能防止同一城市组跨 split，却不能证明任务
模板未见。编排集的自动审计发现 3 个标签相关语义模板同时出现在多个 split，因此
manifest 明确记录：

```json
{"semantic_template_overlap_count":3,"semantic_template_holdout":false}
```

所以 `orchestration-post-training.json` 中 test 24 条的 100% 只应称为
“city-group-held-out synthetic oracle imitation regression”。不能称为 100% 未见任务
泛化、真实用户准确率或生产安全率。

### Policy reranker 的 95% 不是偏好学习能力

`policy_reranker.py` 的标签来自：

```text
用户稳定性权重 × 保留率 + 用户质量权重 × 效用保持率
```

而这些权重、保留率和效用保持率也直接进入模型特征。它本质上在蒸馏一个已知算术
oracle。冻结 test 为 60 个合成样本，线性模型 top-1 为 95%，但直接执行原公式的
`closed_form_oracle_accuracy` 为 100%。因此 95% 不能作为模型发现新偏好规律、模型
泛化或生产收益；最多证明轻量重排器近似了预设公式。该重排器虽在 runtime 使用，硬
约束仍由确定性 Verifier 掌控。

### LoRA 只是 bounded smoke

有证据绑定的四个运行如下：

| 领域 | 阶段 | train / validation | optimizer steps | 关键验证指标 |
| --- | --- | ---: | ---: | --- |
| orchestration | SFT | 192 / 24 | 3 | eval loss 3.2767 |
| orchestration | SFT→DPO | 192 / 24 | 3 | DPO reward accuracy 0.4167 |
| itinerary | SFT | 80 / 20 | 3 | eval loss 2.0948 |
| itinerary | SFT→DPO | 146 / 36 | 3 | DPO reward accuracy 0.6389 |

共同设置是 135M 参数基座、LoRA `r=16`、`alpha=32`、dropout 0.05、
`target_modules=all-linear`。约 19.6 MB 的 adapter 文件存在且曾通过 CPU reload。

这些数字不能解释为下游能力：

- 三步训练远未收敛，目的是证明数据→优化→保存→重载路径可执行；
- eval loss/token accuracy 衡量 teacher-forced token 拟合，不等于可执行行程、工具调用
  正确率、硬约束通过率或用户偏好质量；
- DPO reward accuracy 样本很小，其中 orchestration 0.4167 甚至低于 0.5，不能声称
  preference optimization 成功；
- 没有多随机种子、置信区间、同模型 base/SFT/DPO 生成式盲评或真实用户任务评测；
- `apps/api/src` 没有 `PeftModel` 或 adapter 加载路径，当前线上 LLM 路由使用外部模型
  Provider，不会自动使用这些 LoRA。

## 修正数据与历史 adapter 的关系

数据 v2 修复后，四个 train 文件的 SHA 都与历史 LoRA 证据中记录的训练时 SHA 不同。
因此旧 adapter 仍可作为“历史训练与重载 smoke”证据，但不是“已在修正数据 v2 上训练”
的 adapter。`training/post_training_audit.py` 会同时显示训练时 hash 和当前 hash，并输出：

```json
{"all_match_current_data":false,"corrected_data_adapters_ready":false}
```

未来必须重新训练，且训练脚本会把 train/validation SHA、数据合同版本和 split audit
写入每个 run 的 `tripchord_training_metrics.json`。`collect_lora_evidence.py` 也不再把
当前可变数据文件偷偷绑定到旧 adapter；若运行时指标没有训练时 hash，只能继承与同一
adapter SHA 绑定的历史证据，否则标记 provenance 缺失。

修正数据还暴露了历史长度设置的问题：用 SmolLM2 tokenizer 实测，compact SFT train
为 2815–3546 tokens，80/80 条超过 1024；DPO train 的 292 个 chosen/rejected 序列为
2730–3509 tokens，292/292 超过 1024。4096 上限下两者超限均为 0。训练入口现在会在
正式优化前做 tokenizer 级审计，默认遇到超限直接退出；只有显式
`--allow-truncation` 才允许截断，并把该选择写入 metrics。因此历史 `max_length=1024`
运行不能用于完整上下文能力结论。

## Runtime 接入清单

| 产物 | 当前 API runtime | 权限边界 |
| --- | --- | --- |
| `training/artifacts/replan-policy.json` | 已加载 | 只在 Verifier 已判可行的 local/global 候选间重排 |
| `training/artifacts/orchestration-policy.json` | 未加载 | 离线线性 oracle-imitation 实验 |
| `training/runs/*/adapter_model.safetensors` | 未加载 | 历史 3-step LoRA smoke 权重 |
| 外部 LLM Provider | 配置后加载 | 当前 Agent 的真实模型能力来源，与上述 LoRA 无关 |

“代码里有模型权重”“adapter 能 reload”“runtime 实际加载”“生产流量使用并产生收益”是
四个不同证据等级，本项目目前只对不同产物分别达到前述表格中的等级。

## 可复现检查

```bash
uv run python -m training.build_trace_datasets
uv run python -m training.build_compact_lora_datasets
uv run python -m training.build_orchestration_datasets
uv run python -m training.post_training_audit
uv run python -m training.train_sft --validate-only \
  --train training/data/compact_itinerary_sft_train.jsonl \
  --validation training/data/compact_itinerary_sft_validation.jsonl
uv run python -m training.train_dpo --validate-only \
  --train training/data/compact_itinerary_dpo_train.jsonl \
  --validation training/data/compact_itinerary_dpo_validation.jsonl
```

## 面试声明红线

可以说：完成 SFT/DPO LoRA 的真实三步训练和 adapter reload；修复了 completion 标签
泄漏，增加 split/data provenance gate；轻量 replan reranker 已接入且不能绕过 Verifier。

不能说：LoRA 已接入生产 Agent、后训练让行程质量提升多少、95% 是真实偏好准确率、
100% 是未见任务泛化、DPO 显著优于 SFT、或这些结果来自真实用户/OTA 数据。
