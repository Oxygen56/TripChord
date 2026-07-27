# 简历项目成稿

## TripChord（旅弦）— 可验证的自由行智能规划与动态恢复系统

技术栈：Python / FastAPI / Pydantic / OR-Tools CP-SAT / SQLAlchemy / PostgreSQL /
Redis / React 19 / TypeScript / Vite / TRL / PEFT / Docker / Nginx

- 从 0 设计并实现自由行决策系统，将用户偏好、预算、营业时间、景点容量、跨点移动时间和必去项统一建模为约束优化问题；采用 Planner–Verifier–Repair
  闭环输出可审计计划版本与差异，而非仅生成旅行文案。
- 建设 120 条固定种子规划基准与独立校验器，CP-SAT 在全部场景保持硬约束有效；移除移动时间后有效率降至 0%，移除预算后仅 30.83% 有效；相对确定性
  earliest-fit greedy 的平均效用提升 0.83%，并如实保留 greedy 同样有效的实验结论。
- 实现价格变化、售罄、天气、闭园、延误和用户改需事件的影响域分析与局部重规划；120/120 闭园场景恢复成功，未受影响项保留率 100%，局部修复整体保留率
  83.38%，对比全局重算 17.28%。
- 针对“少改动”与“高效用”的冲突，生成局部/全局两个候选并先通过确定性 Verifier，再由轻量 pairwise policy reranker 选择；按未见城市组切分的测试集
  Top-1 为 95%，优于 always-local 71.67%（合成偏好目标）。
- 设计报价真值模型和多源 Provider Gateway，支持 Amadeus 航班搜索/复价、Booking Demand 住宿搜索/复价、AMap 地点/路线/天气、用户报价快照及 replay；统一携带
  provider、环境、抓取时间、freshness 与 price state，并通过超时/失败注入验证健康结果隔离。
- 完成 React 交互工作区与 FastAPI 控制面：租户隔离、Bearer/API Key、PostgreSQL 版本链、幂等键、任务租约/重试/重启恢复、Redis 限流、SSE 与鉴权轮询、JSON 日志、
  Prometheus 指标、三段 Alembic 迁移及 Docker Compose 部署；58 个 Python 测试、严格类型检查、前端构建/测试和依赖审计通过。
- 从规划/消融/修复轨迹生成 120 条 SFT 与 222 对 DPO 数据，按目的地组隔离 train/validation/test，并实现当前 TRL/PEFT LoRA 训练入口；模型收益仅在完成实际训练和
  unseen-city 评测后声明。

## 面试主线

最初假设是“约束优化器会让简单 greedy 大量失效”，但基准显示 greedy 也达到 100% 硬约束有效，CP-SAT 的平均效用只高 0.83%。因此没有包装虚假的巨大提升，转而通过
消融证明移动时间与预算约束不可省，并把主要创新推进到异常恢复：局部修复能显著保留用户已确认安排，但会损失部分全局效用。最终系统同时生成局部与全局合格候选，再根据用户
稳定性/质量偏好做 verifier-gated 选择。这条“假设—证伪—机制改版—边界化结果”比单纯调用大模型更能体现 AI 工程与系统设计能力。

## 对外声明边界

简历正文可以使用上面的已验证数字。生产供应商覆盖、真人偏好效果、LLM adapter 提升和生产吞吐量暂不写入结果型描述；对应证据与限制见 `docs/claim-ledger.md`。
