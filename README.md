# taleclaw

taleclaw 是一个用 Python 实现的 **Agent 运行时平台**。它不是单一的聊天脚本，而是一套围绕 LLM 对话、工具调用、多用户会话、长期记忆、插件扩展和多入口网关搭建的运行框架，并在其上构建了一个垂直的 **Coding Agent** 应用。

## 核心能力

- **四类入口**：CLI、本地 Web 控制台、Telegram、飞书（Feishu）。
- **三种模式**：普通聊天、混合（Hybrid）模式、Coding 任务模式。
- **模型路由**：OpenAI 兼容调用，按用途（`chat` / `coding` / `summary` / `hybrid` / `scheduled_agent`）路由到不同 provider，支持 provider 健康检查与自动切换。
- **工具调用**：文件读写、bash、memory、storage、sandbox、artifact、scheduler、Markdown 转 PDF、web search 等。
- **多用户隔离**：每个用户拥有独立的 session、memory、storage。
- **长期记忆**：长期记忆、近期上下文、历史摘要、任务会话记忆提升与生命周期管理。
- **代码安全 RAG**：基于 Qdrant + bge-m3 的代码安全知识库与检索路由。
- **可扩展插件**：插件 MVP、工具搜索与延迟加载工具。

## 目录结构

```text
runtime/      # 通用 agent 执行链路（bootstrap、agent_loop、pipeline、context、trace、routing）
models/       # 模型 provider 与模型路由（provider、model_pool、model_task_runner）
agents/       # 平台上的垂直 agent 应用（coding agent 等）
modes/        # 聊天 / 混合 / coding 模式与意图分类
tools/        # 工具 schema、注册表、策略、执行器与处理器
memory/       # 长期记忆、scoped store、生命周期、归档与历史摘要
sessions/     # 会话与会话存储
bus/          # 事件总线（user / team）
gateway/      # Telegram / 飞书网关
web/          # 本地 Web 控制台（标准库 HTTP server + 前端静态资源）
plugins/      # 插件
retrieval/    # 检索
knowledge/    # 知识库内容
evaluation/   # 评测 harness 与 SWE-bench adapter
tests/        # 测试
docs/         # 设计文档（详见 docs/README.md）
```

更完整的结构与运行链路说明见 [docs/overview/PROJECT_STRUCTURE.md](docs/overview/PROJECT_STRUCTURE.md) 与 [docs/overview/CODEBASE_SUMMARY.md](docs/overview/CODEBASE_SUMMARY.md)。

## 快速开始

依赖管理推荐使用 [uv](https://github.com/astral-sh/uv)。

```bash
# 1. 安装依赖
uv venv
uv pip install -e .
# 或者：pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env
# 编辑 .env，至少填好模型 provider 的 API key 和 base url

# 3. 启动 CLI
python cli.py
```

### 本地 Web 控制台

```bash
python web/server.py --host 127.0.0.1 --port 8000
```

> 安全提示：Web server 默认绑定到 `127.0.0.1`。若要对外暴露，请放在带 TLS 终止的反向代理之后，并确保已正确配置登录鉴权（见 [docs/web/WEB_LOGIN_AND_REGISTRATION.md](docs/web/WEB_LOGIN_AND_REGISTRATION.md)）。

### Telegram / 飞书 Worker

```bash
python telegram_worker.py   # 需要在 .env 中配置 Telegram bot token
python feishu_worker.py     # 需要在 .env 中配置飞书应用凭证
```

### Docker Compose

```bash
# 仅启动 Web 控制台 + Qdrant + Postgres
docker compose up agent-console qdrant postgres

# 按需启用网关 worker（profile）
docker compose --profile telegram up
docker compose --profile feishu up
```

## 配置

所有运行时配置通过 `.env` 注入，模板见 [.env.example](.env.example)，其中包含模型 provider、网关凭证、Web 登录、Postgres、Qdrant 等分区说明。**请勿提交真实 `.env`。**

## 测试

```bash
pytest -q
```

## 文档

设计说明、阶段记录与部署文档集中在 [docs/README.md](docs/README.md)。建议先看 `docs/overview/`，再按主题深入系统设计、运行时、记忆、工具插件、Web、RAG 等专题。

## 贡献

欢迎提交 issue 与 PR，提交规范见 [CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证

本项目以 [MIT License](LICENSE) 开源。
