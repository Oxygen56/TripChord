# 本地运行与维护指南

本文面向需要在本机启动、连接真实模型或维护 TripChord 的开发者。普通读者可以先看 [README](../README.md)；下面的命令和环境变量是开发说明，不是用户需要理解的产品功能。

## 环境要求

- Python 3.12 或 3.13；
- Node.js 22；
- [uv](https://docs.astral.sh/uv/)；
- 实时网页查询还需要 Chrome 和仓库内的 [TripChord Companion](../apps/browser-companion/README.md)。

## 最短启动路径

```bash
uv run python scripts/tripchord_launcher.py check
uv run python scripts/tripchord_launcher.py setup
uv run python scripts/tripchord_launcher.py wizard
```

分别启动 API 和网页界面：

```bash
uv run python scripts/tripchord_launcher.py api
uv run python scripts/tripchord_launcher.py web
```

浏览器打开 `http://localhost:5173`。默认配置不会访问真实旅行平台，也不会调用付费模型。

需要分别调试服务时，可以使用：

```bash
uv sync --all-groups
uv run alembic upgrade head
uv run uvicorn tripchord.main:app --reload
npm ci
npm run dev
```

## 连接模型

TripChord 支持 OpenAI-compatible 和 Anthropic 模型接口。下面以兼容 OpenAI 协议的 DeepSeek 服务为例：

```bash
export MODEL_PROVIDER=openai_compatible
export MODEL_BASE_URL=https://api.deepseek.com
export MODEL_NAME=deepseek-v4-flash
export MODEL_API_KEY='<从本机密钥管理工具读取>'
export MODEL_AGENTS_REQUIRED=true
export MODEL_MAX_ATTEMPTS=1
```

模型只参与语言理解、体验判断和结果说明。日期、人数、金额、具体产品、行程是否成立和最终发布仍由程序处理。更完整的边界与当前真实接线情况见 [模型、上下文与偏好记忆](model-context-memory-rag.md)。

需要单独确认模型接口与工具往返是否可用时，可以运行：

```bash
uv run python scripts/run_model_runtime_smoke.py \
  --ack-live-cost \
  --provider openai_compatible \
  --base-url https://api.deepseek.com \
  --model deepseek-v4-flash \
  --output benchmarks/results/model-runtime-smoke-new.json
```

这个命令会产生三次模型请求，只验证模型接口，不代表交通、住宿和接驳的完整实时规划已经成功。

## 连接本机 Chrome

实时网页查询使用用户自己的 Chrome 登录状态。安装并配对 Companion 后，在同一台电脑启动：

```bash
uv run python scripts/start_live_api.py
```

首次启动会在 `.runtime/browser-bridge-token` 生成本机连接密钥。按 Companion 的设置说明完成一次配对后，后续 API 重启会复用同一个连接。TripChord 只读取用户授权的平台页面；不会读取密码或整个 Chrome 用户目录，也不会下单、付款、接受条款或绕过验证码。

提交实时任务前，界面会检查 Companion 是否在线，以及携程、去哪儿和同程的读取能力是否可用。某个平台要求重新登录或验证码时，本次任务跳过该来源，其余来源继续。

各平台当前支持情况见 [平台接入与当前支持情况](providers.md)。

## 复现桌面核心验收

下面的验收使用保存的真实来源数据，不访问平台：

```bash
env -u TRIPCHORD_FORMAL_MODEL_ROLE .venv/bin/pytest -q -s apps/api/tests/test_v1_acceptance.py
```

它从原始中文需求开始，重新执行日期生成、报价整理、完整方案组合、最终核对、偏好记忆和中文修改。结果与已知边界见 [1.0 桌面核心验收](v1.0-acceptance.md)。

## 可选部署

仓库提供 Docker Compose 配置，用于 API、网页、PostgreSQL 和 Redis 的组合部署：

```bash
docker compose up --build
```

容器不会接管宿主机的 Chrome 登录状态，因此真实浏览器查询仍应在 Chrome 所在的电脑直接运行本机 API。Compose 更适合保存数据回放、API 联调和受控展示。

## 常见问题

| 现象 | 先检查什么 |
| --- | --- |
| 页面无法创建实时任务 | API 是否启动，Companion 是否在线，平台域名是否已经授权。 |
| 某个平台没有报价 | 结果是明确无结果、页面仍在查询、登录阻断，还是没有找到信息完整的价格；不要把它们混成“无库存”。 |
| 模型角色没有运行 | `MODEL_PROVIDER` 和 API Key 是否配置；默认模式本来就不会调用外部模型。 |
| 任务中断 | 先从任务编号查询已有状态；不要重复创建同一个长任务。 |
| 最终没有方案 | 查看缺少的是哪一项当前报价或行程关系；TripChord 不会用旧价格或模型猜测补齐。 |

深入排查时再查看 `GET /health`、`GET /ready`、任务 `trace_id` 和 `benchmarks/results/` 中对应运行记录。公开介绍项目时，应把实时查询、保存数据回放和接口单独检查明确区分。
