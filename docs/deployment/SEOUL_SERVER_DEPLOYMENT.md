# Taleclaw 首尔服务器部署手册

这份文档按当前仓库的 Docker Compose 配置重新编写。默认部署只启动 Web 控制台和 PostgreSQL；RAG、Qdrant、Telegram、飞书都作为可选 profile，不在默认部署里启动。

## 目标架构

```text
Internet
  |
  | 80 / 443
  v
Nginx
  |
  | 127.0.0.1:8000
  v
agent-console
  |
  | postgres:5432
  v
postgres
```

默认服务：

```text
agent-console
postgres
```

可选服务：

```text
telegram-worker
feishu-worker
qdrant
```

服务器安全组只需要开放 `22`、`80`、`443`。不要把 `8000`、`8010`、`55432`、`6333`、`6334` 暴露到公网。

## 1. 准备服务器

适用环境：Ubuntu 22.04 / 24.04。

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl gnupg git nginx
```

安装 Docker CE 腾讯云镜像源。下面这段会先清理上一次失败留下的 key/source 文件，再重新下载 key：

```bash
sudo rm -f /etc/apt/keyrings/docker.gpg
sudo rm -f /etc/apt/keyrings/docker.asc
sudo rm -f /etc/apt/sources.list.d/docker.list
sudo rm -f /etc/apt/sources.list.d/docker.sources

sudo install -m 0755 -d /etc/apt/keyrings

sudo curl -fL \
  --retry 5 \
  --retry-delay 2 \
  --retry-all-errors \
  https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc

test -s /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

写入 Docker APT source：

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://mirrors.cloud.tencent.com/docker-ce/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

安装 Docker Engine 和 Compose 插件：

```bash
sudo apt-get update
sudo apt-get install -y \
  docker-ce \
  docker-ce-cli \
  containerd.io \
  docker-buildx-plugin \
  docker-compose-plugin
sudo systemctl enable --now docker
```

可选：让当前用户免 `sudo` 使用 Docker。

```bash
sudo usermod -aG docker "$USER"
newgrp docker
```

## 2. 拉取代码

建议放在 `/opt/taleclaw`。

```bash
sudo mkdir -p /opt/taleclaw
sudo chown "$USER:$USER" /opt/taleclaw
git clone <your-repo-url> /opt/taleclaw
cd /opt/taleclaw
```

如果代码已经在服务器上：

```bash
cd /opt/taleclaw
git pull
```

## 3. 配置 `.env`

```bash
cd /opt/taleclaw
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少设置下面这些项。数据库密码建议只用字母、数字和下划线；如果密码里有 `@`、`:`、`/` 等字符，需要在 `DATABASE_URL` 里做 URL 编码。

```dotenv
# 模型服务：这里以 OpenAI 兼容中转站为例
LLM_PROVIDER=openai_relay
OPENAI_RELAY_API_KEY=replace-with-your-key
OPENAI_RELAY_BASE_URL=https://your-relay.example.com/v1
OPENAI_RELAY_MODEL=gpt-4o-mini
OPENAI_RELAY_WIRE_API=responses
OPENAI_RELAY_MAX_TOKENS_PARAM=max_tokens

# 不使用本机代理
USE_LOCAL_PROXY=0

# PostgreSQL：容器内部访问必须使用 postgres:5432
POSTGRES_DB=agent_console
POSTGRES_USER=agent
POSTGRES_PASSWORD=replace_with_a_strong_db_password
POSTGRES_HOST_PORT=55432
DATABASE_URL=postgresql://agent:replace_with_a_strong_db_password@postgres:5432/agent_console
SESSION_DATABASE_URL=
WEB_AUTH_DATABASE_URL=
MEMORY_ARCHIVE_DATABASE_URL=
GATEWAY_DATABASE_URL=
TRACE_INDEX_ENABLED=1
TRACE_DATABASE_URL=

# Web 登录：公网部署建议关闭注册和匿名访问
WEB_USERNAME=
WEB_PASSWORD=
WEB_USERS_JSON={"admin":{"password":"replace-with-a-strong-admin-password","role":"admin"}}
WEB_ALLOW_REGISTRATION=0
WEB_ALLOW_ANONYMOUS=0
WEB_SESSION_TTL_HOURS=168
WEB_COOKIE_SECURE=0

# Coding agent 的容器内工作目录
WORKSPACE_ROOTS=/app
DEFAULT_CODING_WORKSPACE=/app

# 默认不部署 RAG / Qdrant
RAG_ENABLED=0
HISTORY_VECTOR_ENABLED=0
SECURITY_RAG_AUTO_CONTEXT_ENABLED=0
SECURITY_RAG_PLUGIN_ENABLED=0

# 可选工具 Key
TAVILY_API_KEY=replace-me
```

如果你不用 `openai_relay`，把模型配置改成自己的 provider，并同步调整 `LLM_PROVIDER` 和 `LLM_ROUTE_*`。

## 4. 启动默认服务

先启动数据库：

```bash
sudo docker compose up -d postgres
```

再构建并启动 Web 控制台：

```bash
sudo docker compose up -d --build agent-console
```

检查服务：

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 agent-console
sudo docker compose logs --tail=100 postgres
```

本机健康检查需要登录凭据：

```bash
curl -u admin:replace-with-a-strong-admin-password http://127.0.0.1:8000/api/health
```

如果这里返回 JSON，默认部署已经跑起来了。

## 5. 配置 Nginx

先用 HTTP 反代到本机 `8000`。把 `your.domain.example` 换成你的域名；如果还没有域名，可以先写服务器公网 IP 或 `_`。

```bash
sudo tee /etc/nginx/sites-available/taleclaw >/dev/null <<'NGINX'
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 80;
    server_name your.domain.example;

    client_max_body_size 50m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
    }
}
NGINX

sudo ln -sf /etc/nginx/sites-available/taleclaw /etc/nginx/sites-enabled/taleclaw
sudo nginx -t
sudo systemctl reload nginx
```

浏览器访问：

```text
http://your.domain.example/
```

## 6. 配置 HTTPS

域名 DNS 已经指向服务器后，安装 Certbot：

```bash
sudo apt-get install -y certbot python3-certbot-nginx
sudo certbot --nginx -d your.domain.example
```

证书签好后，把 `.env` 里的 Cookie 安全开关改为：

```dotenv
WEB_COOKIE_SECURE=1
```

重建 Web 控制台：

```bash
sudo docker compose up -d --build agent-console
```

之后使用：

```text
https://your.domain.example/
```

## 7. 更新部署

```bash
cd /opt/taleclaw
git pull
sudo docker compose up -d --build agent-console
sudo docker compose ps
sudo docker compose logs --tail=100 agent-console
```

如果数据库镜像或配置变了，再单独重启 PostgreSQL：

```bash
sudo docker compose up -d postgres
```

## 8. 可选：启用 Telegram

编辑 `.env`：

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-bot-token
TELEGRAM_ALLOWED_USER_IDS=123456789
TELEGRAM_USER_MAP={"123456789":{"user_id":"admin","role":"admin"}}
TELEGRAM_NOTIFY_CHAT_IDS=123456789
```

启动：

```bash
sudo docker compose --profile telegram up -d --build telegram-worker
sudo docker compose logs -f telegram-worker
```

Web 控制台和 PostgreSQL 仍然按默认服务运行。

## 9. 可选：启用飞书

编辑 `.env`：

```dotenv
FEISHU_APP_ID=replace-with-app-id
FEISHU_APP_SECRET=replace-with-app-secret
FEISHU_VERIFICATION_TOKEN=replace-with-verification-token
FEISHU_CALLBACK_HOST=0.0.0.0
FEISHU_CALLBACK_PORT=8010
FEISHU_CALLBACK_PATH=/feishu/events
FEISHU_ALLOWED_OPEN_IDS=ou_xxx
FEISHU_USER_MAP={"ou_xxx":{"user_id":"admin","role":"admin"}}
```

启动：

```bash
sudo docker compose --profile feishu up -d --build feishu-worker
sudo docker compose logs -f feishu-worker
```

如果飞书回调和 Web 控制台共用同一个域名，在 Nginx 的 `server` 块里，把下面这个 `location` 放在 `location /` 前面：

```nginx
location = /feishu/events {
    proxy_pass http://127.0.0.1:8010/feishu/events;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_read_timeout 60s;
    proxy_send_timeout 60s;
}
```

然后：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

## 10. 可选：启用 RAG / Qdrant

当前默认部署不启用 RAG。只有确认要恢复历史向量或安全知识库检索时，再打开这一段。

编辑 `.env`：

```dotenv
RAG_ENABLED=1
HISTORY_VECTOR_ENABLED=1
SECURITY_RAG_AUTO_CONTEXT_ENABLED=1
SECURITY_RAG_PLUGIN_ENABLED=1
QDRANT_URL=http://qdrant:6333
SECURITY_RAG_QDRANT_URL=http://qdrant:6333
```

构建时安装 RAG 依赖，并启动 Qdrant profile：

```bash
sudo env INSTALL_RAG_DEPS=1 RAG_ENABLED=1 docker compose --profile rag up -d --build qdrant agent-console
```

Qdrant Dashboard 只监听本机：

```text
http://127.0.0.1:6333/dashboard
```

不要把 Qdrant 端口开放到公网。

## 11. 备份

创建备份目录：

```bash
mkdir -p /opt/taleclaw-backups
```

备份 PostgreSQL：

```bash
cd /opt/taleclaw
sudo docker compose exec -T postgres pg_dump -U agent -d agent_console \
  > /opt/taleclaw-backups/agent_console_$(date +%F).sql
```

备份本地状态文件：

```bash
cd /opt/taleclaw
sudo tar -czf /opt/taleclaw-backups/taleclaw_files_$(date +%F).tgz \
  .env storage memory .sessions .task_sessions .tasks .team .transcripts .users .gateway
```

如果启用了 RAG，也备份：

```bash
cd /opt/taleclaw
sudo tar -czf /opt/taleclaw-backups/taleclaw_rag_$(date +%F).tgz qdrant_storage
```

## 12. 常用排障

查看默认会启动哪些服务：

```bash
sudo docker compose config --services
```

预期输出：

```text
agent-console
postgres
```

查看所有 profile 服务：

```bash
sudo docker compose --profile rag --profile telegram --profile feishu config --services
```

Web 起不来：

```bash
sudo docker compose logs --tail=200 agent-console
```

数据库连不上时，优先检查 `.env` 里的 `DATABASE_URL`。在容器内必须是：

```dotenv
DATABASE_URL=postgresql://agent:your-password@postgres:5432/agent_console
```

不要在容器内写 `127.0.0.1:55432`，那是宿主机访问 PostgreSQL 的端口。

登录失败时，检查：

```bash
sudo docker compose exec agent-console env | grep -E 'WEB_USERS_JSON|WEB_ALLOW|WEB_COOKIE'
```

模型调用失败时，检查：

```bash
sudo docker compose exec agent-console env | grep -E 'LLM|OPENAI|DEEPSEEK|MIMO|GEMINI'
```

构建突然很慢，确认 RAG 没被误打开：

```bash
sudo docker compose exec agent-console env | grep -E 'RAG_ENABLED|HISTORY_VECTOR|SECURITY_RAG'
```

端口占用时：

```bash
sudo ss -lntp | grep -E ':80|:443|:8000|:8010|:55432'
```

## 13. 上线检查清单

- `sudo docker compose ps` 里 `agent-console` 和 `postgres` 都是 running。
- `curl -u admin:你的密码 http://127.0.0.1:8000/api/health` 返回 JSON。
- Nginx `sudo nginx -t` 通过。
- 公网只开放 `22`、`80`、`443`。
- `.env` 权限是 `600`，没有提交到 Git。
- 公网部署时 `WEB_ALLOW_REGISTRATION=0`、`WEB_ALLOW_ANONYMOUS=0`。
- HTTPS 可用后 `WEB_COOKIE_SECURE=1`。
- 默认部署保持 `RAG_ENABLED=0`，不启动 `qdrant`。
