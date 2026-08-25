# TripChord 文档导航

第一次了解项目，建议按下面顺序阅读：

1. [项目首页](../README.md)：TripChord 解决什么问题、当前能做什么、一次实际行程结果；
2. [简历与面试项目说明](resume-project.md)：一页看懂项目亮点、Agent 分工、关键取舍和 GPT 面试提示词；
3. [最近一次真实只读运行](real-run-maldives-2026-08-24.md)：TripChord 实际查了什么、给出了什么、哪些仍不能声称；
4. [复杂自由行目标架构](target-architecture.md)：2.0 已采用的多城市、分组同行、活动、按需 Agent、记忆和组合求解设计；
5. [当前系统架构](architecture.md)：2.0 中模型角色和普通程序怎样分工；
6. [重要架构决策](architecture-decisions.md)：为什么这样设计，以及哪些方案已经比较后没有采用；
7. [TripChord 2.0 产品需求](product-requirements.md)：完整产品要求、七次纵向迭代和每轮端到端完成标准；
8. [TripChord 2.0 七轮公共验收](v2.0-acceptance.md)：每轮用户能力、当前/冻结证据和准确边界；
9. [产品形态与实施路线](roadmap.md)：2.0 已完成的形态和后续成熟化方向；
10. [1.0 桌面核心验收](v1.0-acceptance.md)：2.0 仍保留的历史桌面核心基线。

按主题继续阅读：

- [TripChord 为什么叫这个名字](naming.md)
- [平台接入与当前支持情况](providers.md)
- [模型、上下文与偏好记忆](model-context-memory-rag.md)
- [本地运行与维护指南](operations.md)
- [架构面试指南](interview-guide.md)

`benchmarks/`、`phase-reviews/`、`done-gate.md`、`claim-ledger.md` 等文件保存特定开发阶段的实验、失败和复核记录。它们用于追溯某次技术结论，可能包含代码名称、运行编号和当时的完成标准，不是面向第一次访问者的产品介绍；当前产品状态应以项目首页和 [2.0 公共验收](v2.0-acceptance.md) 为准。

保存数据回放和旧演示材料只用于重现历史运行，不代表当前平台价格，也不是 TripChord 的用户界面。
