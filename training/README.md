# TripChord 后训练

训练分成两类能力，并分别保留数据、轻量策略结果和实际 LoRA 制品：

1. **编排与裁决**：决定接受、例外确认或重新规划/暂停，以及何时触发 Repair；
2. **行程生成与修复**：在预算、时间窗、必去项和稳定性目标下生成/选择行程。

## 数据边界

- 标签来自固定种子确定性合成 oracle，不是人工偏好或真人订单；
- 城市组 0–7 / 8–9 / 10–11 分别作为 train / validation / test，同一城市组不跨集合；
- 数据合同 `post-training-data-v2` 会拒绝 completion 标签标记、chosen/rejected schema
  不一致、重复 ID、城市组交叉和精确 prompt 交叉；
- compact 数据保留 availability 与 travel matrix，避免输入缺少精确排程所需事实；它只是
  相对紧凑表示，不保证每条记录必然落入 1024-token 上限，截断风险须由 tokenizer 审计；
- DPO prompt 与 completion 之间加入显式分隔符，避免 `}{` 边界的 BPE 合并告警。

## 可复现实验

```bash
# 生成并校验数据
uv run python -m training.build_trace_datasets
uv run python -m training.build_orchestration_datasets
uv run python -m training.build_compact_lora_datasets
uv run python -m training.train_sft --validate-only
uv run python -m training.train_dpo --validate-only

# 完整合成策略对照
uv run python -m training.policy_reranker
uv run python -m training.orchestration_policy
```

轻量策略结果只属于合成 oracle-imitation regression：编排城市组 held-out 为 24 条，且
标签相关语义模板跨 split；行程恢复 reranker 的 test Top-1 为 95%，但直接执行标签生成
公式为 100%。它们不能解释为 LLM 能力、未见任务泛化、真人满意度或生产收益。

## 实际 LoRA 路径

基础模型固定为 `HuggingFaceTB/SmolLM2-135M-Instruct`。选择它是为了在本机跑通当前
Transformers/TRL/PEFT 训练链路；模型主要面向英语，小规模 3-step 结果不作为生产中文质量
声明。

```bash
uv sync --extra training

# 编排 SFT
uv run python -m training.train_sft \
  --model HuggingFaceTB/SmolLM2-135M-Instruct \
  --train training/data/orchestration_sft_train.jsonl \
  --validation training/data/orchestration_sft_validation.jsonl \
  --output training/runs/orchestration-sft-lora \
  --max-steps 3 --max-length 512

# 在 SFT adapter 上继续编排 DPO（保存的 adapter 同时包含 SFT+DPO 更新）
uv run python -m training.train_dpo \
  --model HuggingFaceTB/SmolLM2-135M-Instruct \
  --adapter training/runs/orchestration-sft-lora \
  --train training/data/orchestration_dpo_train.jsonl \
  --validation training/data/orchestration_dpo_validation.jsonl \
  --output training/runs/orchestration-sft-dpo-lora \
  --max-steps 3 --max-length 512

# 修正后的紧凑行程 SFT/DPO 使用相同方式，SmolLM2 tokenizer 审计要求
# max-length 至少覆盖当前最大 3546 tokens，例如 max-length=4096。
```

`--adapter` 使用可训练的现有 PEFT adapter 原位继续优化；不能先 merge 后只保存一个新的
DPO delta，否则重新加载时会丢失 SFT 增量。每个成功运行目录包含 adapter、tokenizer、
trainer state 与 `tripchord_training_metrics.json`；最终哈希和重载结果写入
`benchmarks/results/lora-training-evidence.json`。

当前 checked-in adapter 是数据合同 v2 之前的历史三步 smoke，训练时数据 SHA 与修正后
数据不同。运行 `uv run python -m training.post_training_audit` 可查看 provenance；在重新
训练前不得声称这些 adapter 已基于 v2 数据。完整边界见
`docs/post-training-boundaries.md`。

实际训练会在启动前使用所选模型 tokenizer 审计完整序列；默认不允许静默截断。只有显式
传入 `--allow-truncation` 才能绕过，此时该事实也会写入 run metrics。

## 安全边界

后训练模型只能提出候选、工具计划和返工建议。它不能覆盖确定性 Verifier、偏好宪法或
L3 授权门；只有已经通过硬约束校验的候选才能进入学习排序器。
