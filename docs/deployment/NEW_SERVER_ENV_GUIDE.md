# TaleClaw 新服务器 `.env` 修改手册

适用于 Ubuntu 22.04/24.04、Docker Compose 部署，默认项目目录为 `/opt/taleclaw`。如果你的代码不在该目录，请替换命令中的路径。Docker、Nginx 和 HTTPS 的完整安装步骤见 [首尔服务器完整部署手册](SEOUL_SERVER_DEPLOYMENT.md)。

## 1. 创建或备份 `.env`

进入项目目录：

```bash
cd /opt/taleclaw
```

首次部署：

```bash
cp .env.example .env
chmod 600 .env
nano .env
```

如果 `.env` 已经存在，先备份到项目目录外：

```bash
sudo install -d -m 700 /opt/taleclaw-env-backups
backup_file="/opt/taleclaw-env-backups/taleclaw.env.$(date +%Y%m%d-%H%M%S)"
install -m 600 .env "$backup_file"
nano .env
```

不要提交、打印或发送真实 `.env`。仓库已忽略 `.env`，但项目目录中的其他备份文件不一定会被忽略，因此备份应放在项目目录外。

建议用以下命令分别生成数据库密码和管理员密码：

```bash
openssl rand -hex 24
```

每次运行生成一个不同密码。生成后立即保存到密码管理器，不要粘贴到工单、聊天或公开日志。

## 2. 最小生产配置

下面是一套默认只启动 Web 控制台和 PostgreSQL 的配置。把所有 `REPLACE_*` 替换成真实值。

```dotenv
# ---------------------------------------------------------------------------
# 模型 Provider：必须修改
# ---------------------------------------------------------------------------

LLM_PROVIDER=openai_relay
OPENAI_RELAY_API_KEY=REPLACE_MODEL_API_KEY
OPENAI_RELAY_BASE_URL=https://REPLACE_MODEL_HOST/v1
OPENAI_RELAY_MODEL=REPLACE_MODEL_NAME
OPENAI_RELAY_USER_AGENT=
OPENAI_RELAY_WIRE_API=responses
OPENAI_RELAY_MAX_TOKENS_PARAM=max_tokens
OPENAI_RELAY_CONTEXT_WINDOW_TOKENS=128000
OPENAI_RELAY_MAX_INPUT_TOKENS=
OPENAI_RELAY_MAX_OUTPUT_TOKENS=
OPENAI_RELAY_OUTPUT_RESERVE_TOKENS=

# 单 Provider 部署。未单独配置的用途会回退到 DEFAULT。
LLM_ROUTE_DEFAULT=openai_relay
LLM_ROUTE_CHAT=openai_relay
LLM_ROUTE_CODING=openai_relay
LLM_ROUTE_SUMMARY=openai_relay
LLM_ROUTE_HYBRID=openai_relay
LLM_ROUTE_COMPACT=openai_relay
LLM_ROUTE_TEAMMATE=openai_relay
LLM_ROUTE_REFLECTION=openai_relay
LLM_ROUTE_TASK_CONCLUSION=openai_relay
LLM_ROUTE_FALLBACK=

LLM_HEALTHCHECK_ON_STARTUP=0
USE_LOCAL_PROXY=0

# ---------------------------------------------------------------------------
# PostgreSQL：必须修改两个 REPLACE_DB_PASSWORD，值必须完全对应
# ---------------------------------------------------------------------------

POSTGRES_DB=agent_console
POSTGRES_USER=agent
POSTGRES_PASSWORD=REPLACE_DB_PASSWORD
POSTGRES_HOST_PORT=55432
POSTGRES_IMAGE=postgres:16

# 容器内必须使用 postgres:5432，不能写 127.0.0.1:55432。
DATABASE_URL=postgresql://agent:REPLACE_DB_PASSWORD@postgres:5432/agent_console

# 留空时复用 DATABASE_URL。
SESSION_DATABASE_URL=
WEB_AUTH_DATABASE_URL=
MEMORY_ARCHIVE_DATABASE_URL=
GATEWAY_DATABASE_URL=
TRACE_INDEX_ENABLED=1
TRACE_DATABASE_URL=

# ---------------------------------------------------------------------------
# Web 登录：必须修改管理员密码
# ---------------------------------------------------------------------------

# 使用 WEB_USERS_JSON 时，旧的单用户配置保持为空。
WEB_USERNAME=
WEB_PASSWORD=
WEB_USERS_JSON={"admin":{"password":"REPLACE_ADMIN_PASSWORD","role":"admin"}}
WEB_ALLOW_REGISTRATION=0
WEB_ALLOW_ANONYMOUS=0
WEB_SESSION_TTL_HOURS=168

# HTTP 初次部署先用 0；HTTPS 配置完成后改成 1，并重建 agent-console。
WEB_COOKIE_SECURE=0
WEB_MAX_BODY_BYTES=52428800
AGENT_RUNTIME_STARTUP_TIMEOUT_SECONDS=15
WEB_AGENT_REPLY_TIMEOUT_SECONDS=180

# ---------------------------------------------------------------------------
# 容器工作区：建议保持
# ---------------------------------------------------------------------------

# /opt/taleclaw 是宿主机路径；容器内项目路径是 /app。
WORKSPACE_ROOTS=/app
DEFAULT_CODING_WORKSPACE=/app

# ---------------------------------------------------------------------------
# TaskState 与动态上下文：建议保持
# ---------------------------------------------------------------------------

# CODING_CONTEXT_STATE_ENABLED 是迁移期兼容开关；当前仍需与 TaskState 一起开启。
CODING_CONTEXT_STATE_ENABLED=1
TASK_STATE_CONTEXT_ENABLED=1
SEMANTIC_COMPACTION_ENABLED=1
ARTIFACT_OFFLOADING_ENABLED=1
DYNAMIC_PROMPT_BUDGET_ENABLED=1

PROMPT_SOFT_COMPACTION_RATIO=0.70
PROMPT_COMPACTION_TARGET_RATIO=0.45
PROMPT_HARD_INPUT_RATIO=0.92

# 0 表示不指定固定 token 数，由运行时使用模型窗口的 3% 作为安全余量。
PROMPT_SAFETY_MARGIN_TOKENS=0

LONG_CONTENT_MAX_TOKENS=4000
LONG_CONTENT_MAX_CHARS=20000
LONG_CONTENT_MAX_BYTES=64000

# 使用已经挂载到宿主机的 storage，避免重建容器后 Artifact 丢失。
CONTEXT_ARTIFACT_ROOT=/app/storage/context-artifacts

WORKING_MEMORY_CHECKPOINT_ENABLED=1
WORKING_MEMORY_RESUME_ENABLED=1
WORKING_MEMORY_CONTEXT_BUDGET=4000

# ---------------------------------------------------------------------------
# 默认关闭的可选功能
# ---------------------------------------------------------------------------

RAG_ENABLED=0
HISTORY_VECTOR_ENABLED=0
SECURITY_RAG_AUTO_CONTEXT_ENABLED=0
SECURITY_RAG_PLUGIN_ENABLED=0
INSTALL_RAG_DEPS=0

TELEGRAM_BOT_TOKEN=
FEISHU_APP_ID=
FEISHU_APP_SECRET=
TAVILY_API_KEY=

# ---------------------------------------------------------------------------
# Docker 构建：通常保持默认
# ---------------------------------------------------------------------------

PYTHON_IMAGE=python:3.12-slim
PIP_INDEX_URL=https://pypi.org/simple
PIP_EXTRA_INDEX_URL=
PIP_TRUSTED_HOST=
PIP_DEFAULT_TIMEOUT=180
PIP_RETRIES=10
REQUIREMENTS_FILE=requirements-deploy.txt
```

## 3. 必须修改的配置

### 3.1 模型服务

至少修改：

- `OPENAI_RELAY_API_KEY`
- `OPENAI_RELAY_BASE_URL`
- `OPENAI_RELAY_MODEL`
- `OPENAI_RELAY_USER_AGENT`（可选，仅用于中转网关按客户端标识放行的兼容场景）
- `OPENAI_RELAY_CONTEXT_WINDOW_TOKENS`

`OPENAI_RELAY_BASE_URL` 通常写到 `/v1`，不要写成 `/v1/chat/completions`。

如果自建中转网关明确要求客户端标识，可以设置：

```dotenv
OPENAI_RELAY_USER_AGENT=codex-cli
```

该值只会作为对应 Provider 的 `User-Agent` 请求头发送。不要在其中放入 API Key、Cookie 或其他认证信息；未配置时保持 SDK 的默认请求头。

如果服务支持 OpenAI Responses API，使用：

```dotenv
OPENAI_RELAY_WIRE_API=responses
```

如果服务只支持传统 Chat Completions API，改为：

```dotenv
OPENAI_RELAY_WIRE_API=chat_completions
```

上下文窗口必须按实际模型填写。写得过大可能导致请求被 Provider 拒绝，写得过小则会过早压缩上下文。

### 3.2 数据库密码

最简单安全的做法是使用 `openssl rand -hex 24` 生成只包含十六进制字符的密码，然后将同一个值分别写入：

```dotenv
POSTGRES_PASSWORD=同一个密码
DATABASE_URL=postgresql://agent:同一个密码@postgres:5432/agent_console
```

如果密码包含 `@`、`:`、`/`、`#` 等字符：

- `POSTGRES_PASSWORD` 填原始密码；
- `DATABASE_URL` 中的密码部分必须做 URL 百分号编码。

容器内地址与宿主机地址不要混用：

| 使用位置 | 地址 |
|---|---|
| `agent-console` 容器内 | `postgres:5432` |
| 宿主机上的 `psql` 或调试工具 | `127.0.0.1:55432` |

### 3.3 Web 管理员

公网部署推荐只使用 `WEB_USERS_JSON`，不要同时保留有效的 `WEB_USERNAME` 和 `WEB_PASSWORD`。

```dotenv
WEB_USERNAME=
WEB_PASSWORD=
WEB_USERS_JSON={"admin":{"password":"你的强密码","role":"admin"}}
WEB_ALLOW_REGISTRATION=0
WEB_ALLOW_ANONYMOUS=0
```

密码如果包含双引号或反斜杠，必须按 JSON 规则转义。为了减少配置错误，推荐使用十六进制随机密码。

## 4. 新上下文配置说明

当前 Coding 主路径使用 TaskState 和动态 Prompt 预算：

- `TASK_STATE_CONTEXT_ENABLED=1`：启用 TaskState 权威上下文。
- `SEMANTIC_COMPACTION_ENABLED=1`：允许压缩历史事件并更新结构化状态。
- `ARTIFACT_OFFLOADING_ENABLED=1`：大型正文写入 Artifact，只在 Prompt 中保留引用。
- `DYNAMIC_PROMPT_BUDGET_ENABLED=1`：按实际模型窗口、系统提示、工具定义和输出预留动态计算预算。
- `PROMPT_*_RATIO`：默认在可用输入的 70% 触发压缩，压到 45%，92% 为硬输入边界。
- `LONG_CONTENT_MAX_*`：正文超过任一 token、字符或字节阈值时进行外置。

不要再配置以下旧固定压缩参数，它们已经不决定当前 TaskState 的压缩行为：

```text
CODING_CONTEXT_COMPACTION_TRIGGER_TOKENS
CODING_CONTEXT_COMPACTION_TARGET_TOKENS
CODING_CONTEXT_RECENT_GROUPS
```

### Artifact 持久化

代码默认目录是 `/app/.coding_applications/artifacts`，但当前 Compose 没有单独挂载 `/app/.coding_applications`。重建容器时，这个默认目录可能丢失。

因此生产环境建议显式设置：

```dotenv
CONTEXT_ARTIFACT_ROOT=/app/storage/context-artifacts
```

Compose 已将宿主机的 `./storage` 挂载为容器内 `/app/storage`，所以实际文件会保存在：

```text
/opt/taleclaw/storage/context-artifacts
```

## 5. 修改完成后检查

检查权限和 Git 忽略状态：

```bash
cd /opt/taleclaw
stat -c '%a %n' .env
git check-ignore .env
```

权限应为 `600`，`git check-ignore` 应输出 `.env`。

检查必填项是否缺失或仍是占位符。这个命令只输出状态，不输出秘密值：

```bash
python3 - <<'PY'
from pathlib import Path

required = {
    "OPENAI_RELAY_API_KEY",
    "OPENAI_RELAY_BASE_URL",
    "OPENAI_RELAY_MODEL",
    "POSTGRES_PASSWORD",
    "DATABASE_URL",
    "WEB_USERS_JSON",
}

values = {}
for raw_line in Path(".env").read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    name, value = line.split("=", 1)
    values[name.strip()] = value.strip()

failed = False
for name in sorted(required):
    value = values.get(name, "")
    ok = bool(value) and "REPLACE_" not in value and "replace-" not in value
    print(f"{name}: {'OK' if ok else 'MISSING_OR_PLACEHOLDER'}")
    failed = failed or not ok

raise SystemExit(1 if failed else 0)
PY
```

检查 Compose 能否解析配置，但不要输出展开后的完整配置：

```bash
sudo docker compose config --quiet
```

## 6. 首次启动

先启动 PostgreSQL：

```bash
cd /opt/taleclaw
sudo docker compose up -d postgres
sudo docker compose exec postgres pg_isready -U agent -d agent_console
```

数据库 ready 后构建并启动 Web：

```bash
sudo docker compose up -d --build agent-console
sudo docker compose ps
sudo docker compose logs --tail=100 agent-console
```

在服务器本机检查登录页：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/login
```

应返回 `200` 或正常重定向状态。

检查容器是否收到必要变量，但不输出它们的内容：

```bash
sudo docker compose exec agent-console python -c '
import os
names = ["OPENAI_RELAY_API_KEY", "DATABASE_URL", "WEB_USERS_JSON"]
for name in names:
    print(name + ": " + ("set" if os.getenv(name) else "MISSING"))
'
```

检查非敏感上下文开关：

```bash
sudo docker compose exec agent-console python -c '
import os
names = [
    "TASK_STATE_CONTEXT_ENABLED",
    "SEMANTIC_COMPACTION_ENABLED",
    "ARTIFACT_OFFLOADING_ENABLED",
    "DYNAMIC_PROMPT_BUDGET_ENABLED",
    "CONTEXT_ARTIFACT_ROOT",
]
for name in names:
    print(name + "=" + os.getenv(name, "<unset>"))
'
```

## 7. 以后修改 `.env` 怎么生效

普通运行时配置改变后，重建对应容器：

```bash
cd /opt/taleclaw
sudo docker compose up -d --force-recreate agent-console
```

如果启用了 Telegram 或飞书，也要重建对应 worker：

```bash
sudo docker compose --profile telegram up -d --force-recreate telegram-worker
sudo docker compose --profile feishu up -d --force-recreate feishu-worker
```

如果修改了 `PYTHON_IMAGE`、`PIP_*`、`REQUIREMENTS_FILE` 或 `INSTALL_RAG_DEPS` 等镜像构建参数，需要重新构建：

```bash
sudo docker compose up -d --build --force-recreate agent-console
```

HTTPS 已经配置完成后：

```dotenv
WEB_COOKIE_SECURE=1
```

然后执行：

```bash
sudo docker compose up -d --force-recreate agent-console
```

## 8. 常见问题

| 现象 | 常见原因 | 处理方式 |
|---|---|---|
| Web 日志显示数据库连接失败 | `DATABASE_URL` 在容器内写成了 `127.0.0.1:55432` | 改为 `postgres:5432`，重建 `agent-console` |
| 新数据库密码不生效 | `postgres_data/` 已存在，PostgreSQL 不会用新 env 自动修改已有用户 | 按完整部署手册的“4.5 修改数据库密码”先修改数据库用户，再同步 `.env` |
| 模型返回 404 | Base URL 写到了具体接口，或 wire API 不匹配 | Base URL 通常写到 `/v1`；在 `responses` 与 `chat_completions` 间选择正确协议 |
| 模型或路由启动时报未知 Provider | `LLM_ROUTE_*` 引用了没有 API Key/Profile 的名称 | 单 Provider 部署全部写 `openai_relay`，或完整配置对应 Provider |
| Coding Agent 找不到工作区 | 把宿主机 `/opt/taleclaw` 写进了容器配置 | `WORKSPACE_ROOTS` 和 `DEFAULT_CODING_WORKSPACE` 使用 `/app` |
| Artifact 在容器重建后消失 | 使用了未挂载的默认目录 | 设置 `CONTEXT_ARTIFACT_ROOT=/app/storage/context-artifacts` |
| 修改 `.env` 后行为没变化 | 旧容器仍持有创建时的环境变量 | 使用 `docker compose up -d --force-recreate agent-console` |
| 开启 HTTPS 后反复掉登录 | `WEB_COOKIE_SECURE` 与实际协议不一致 | HTTPS 下设为 `1`；纯 HTTP 调试时设为 `0` |
| 构建时间突然很长 | 错误开启 RAG 依赖 | 默认保持 `INSTALL_RAG_DEPS=0`、`RAG_ENABLED=0` |

## 9. 可选功能

默认部署不要启动不需要的 profile：

- Telegram：[Telegram Gateway](../gateways/TELEGRAM_GATEWAY.md)
- 飞书：[Feishu Gateway](../gateways/FEISHU_GATEWAY.md)
- RAG/Qdrant：参见[首尔服务器完整部署手册](SEOUL_SERVER_DEPLOYMENT.md)中的可选 profile 章节
- TaskState 数据迁移：[TaskState Context 迁移 Runbook](../migrations/TASK_STATE_CONTEXT_MIGRATION.md)

完成 `.env` 配置后，继续按[首尔服务器完整部署手册](SEOUL_SERVER_DEPLOYMENT.md)配置 Nginx、HTTPS、备份和上线检查。
