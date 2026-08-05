# 参与贡献

感谢你愿意改进 TripChord。这个项目优先接受能够提升可验证性、安全边界和真实规划质量的贡献。

## 开始之前

1. 阅读 `README.md`、`AGENTS.md`、`docs/architecture.md` 和 `docs/claim-ledger.md`。
2. 对较大的功能或新的平台适配器，先创建 Issue 说明目标、数据来源、权限和验收方法。
3. 不要提交真实 API Key、Cookie、账号数据、用户行程、平台页面原文或未经脱敏的截图。

## 本地开发

```bash
uv sync --locked --all-groups
npm ci
uv run alembic upgrade head
```

提交前运行：

```bash
uv run ruff check .
uv run mypy apps/api/src
uv run pytest
npm run build
npm test
docker compose config
```

## 设计约束

- 硬约束、金额、时间与来源校验必须保持确定性和可测试。
- LLM 适合需求理解、搜索策略、候选评议和解释，不得成为事实来源。
- 每次 Repair 都要输出差异，并默认保留未受影响的行程组件。
- 新增外部事实必须携带 provider、采集时间、数据模式和 freshness。
- 新平台连接必须最小权限、只读、由用户主动授权，并在登录、验证码或 DOM 漂移时失败关闭。
- 不得把回放、缓存、估算或用户导入报价标注为实时可预订价格。

## Pull Request

PR 请说明：

- 修改了什么以及为什么；
- 对用户或开发者的影响；
- 新增或改变了哪些权限和外部数据来源；
- 使用了哪些测试与评测；
- 哪些结论仍未得到真实环境验证。

修复规划失败时，请尽量增加冻结回归场景。不要为了让测试通过而放宽事实、权限或 Verifier 边界。
