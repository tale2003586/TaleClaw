# taleclaw 首尔服务器完整部署手册

## 1. 适用范围

这份手册用于在一台新的首尔 Ubuntu 云服务器上部署 taleclaw：

```text
Internet
   |
Nginx :80/:443
   |
agent-console 127.0.0.1:8000

scheduler-worker     独立后台定时任务
telegram-worker      独立 Telegram 长轮询，可选启用
```

推荐系统：

```text
Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS
2 核 CPU
2 GB 内存
20 GB 磁盘
```

最低可以使用 1 GB 内存，但执行 Coding 任务和 Docker 构建时更容易吃紧。

本文提供两种 Web 上线方式：

- 暂时没有域名：使用 `http://公网IP`
- 已有域名：使用 Nginx + Certbot 配置 `https://你的域名`

公网 IP 的 HTTP 方式只适合首次验收。HTTP 不会加密登录密码和 Cookie，不建议长期使用；
准备正式使用 Web 页面时，应尽快配置域名和 HTTPS。

Telegram 使用 `getUpdates` 长轮询，只需要服务器可以向外访问
`https://api.telegram.org:443`，不需要开放新的入站端口。首尔节点通常可以直连 Telegram；
仍然建议在正式启动前执行本文的连通性检查。

## 2. 准备信息

开始前准备：

| 名称 | 示例 | 用途 |
| --- | --- | --- |
| 公网 IP | `203.0.113.10` | SSH 和无域名访问 |
| SSH 用户 | `ubuntu` | 登录服务器 |
| Git 仓库 | `https://github.com/tale2003586/learn-claude.git` | 拉取代码 |
| 模型 API Key | `sk-...` | Agent 模型调用，DeepSeek 或 MiMo 均可 |
| 管理员密码 | 自行生成强密码 | Web 管理员登录 |
| Tavily API Key | 可选 | Web search 插件 |
| Telegram Bot Token | 可选 | Telegram Gateway |
| 域名 | 可选，例如 `bot.example.com` | HTTPS |

管理员密码建议使用至少 16 位字母、数字、短横线和下划线组合。不要把真实密码、API Key
或 Telegram Bot Token 提交到 Git。

## 3. 云安全组

在云厂商控制台中配置入站规则：

| 端口 | 来源 | 用途 |
| --- | --- | --- |
| `22/tcp` | 你的固定 IP，或临时使用 `0.0.0.0/0` | SSH |
| `80/tcp` | `0.0.0.0/0` | HTTP 和 Certbot 验证 |
| `443/tcp` | `0.0.0.0/0` | HTTPS |

不要向公网开放：

```text
8000/tcp
```

项目中的 Compose 已经将后端绑定到：

```text
127.0.0.1:8000
```

Telegram Gateway 不需要新增入站规则。

## 4. SSH 登录

在本地终端执行：

```bash
ssh ubuntu@你的公网IP
```

登录后确认系统：

```bash
cat /etc/os-release
uname -m
```

## 5. 更新系统并安装基础工具

```bash
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl git nginx snapd
sudo systemctl enable --now nginx
```

检查 Nginx：

```bash
sudo systemctl status nginx --no-pager
```

## 6. 安装 Docker Engine 和 Compose

下面使用 Docker 官方 Ubuntu APT 仓库。

先删除可能冲突的旧包。新服务器提示未安装属于正常现象：

```bash
sudo apt-get remove -y docker.io docker-compose docker-compose-v2 docker-doc podman-docker containerd runc
```

添加 Docker 官方 GPG Key：

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
```

添加软件源：

```bash
sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: $(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")
Components: stable
Architectures: $(dpkg --print-architecture)
Signed-By: /etc/apt/keyrings/docker.asc
EOF
```

安装：

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

验证：

```bash
sudo docker run --rm hello-world
sudo docker compose version
```

本文后续统一使用 `sudo docker`，不要求修改 Docker Socket 权限。

## 7. 拉取代码

新服务器只能获取已经推送到远端仓库的代码。先在开发机确认本次改动已经提交并推送：

```bash
git status --short
git add .
git commit -m "Add Telegram gateway and deployment docs"
git push
```

然后回到首尔服务器，创建固定目录：

```bash
mkdir -p ~/apps
cd ~/apps
git clone https://github.com/tale2003586/learn-claude.git taleclaw
cd ~/apps/taleclaw
```

如果你的仓库地址不同，替换 `git clone` 后面的地址。

确认关键文件：

```bash
ls -la Dockerfile docker-compose.yml .env.example
```

## 8. 创建环境变量

复制模板：

```bash
cd ~/apps/taleclaw
cp .env.example .env
nano .env
```

### 8.1 最小 Web 配置

先填写：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=替换为你的DeepSeekKey
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=deepseek-v4-flash
USE_LOCAL_PROXY=0

WEB_USERS_JSON={"admin":{"password":"替换为管理员强密码","role":"admin"}}
WEB_ALLOW_REGISTRATION=0
WEB_ALLOW_ANONYMOUS=0
WEB_SESSION_TTL_HOURS=168
WEB_COOKIE_SECURE=0
WEB_MAX_BODY_BYTES=52428800

TAVILY_API_KEY=替换为你的TavilyKey
SCHEDULER_TIMEZONE=Asia/Shanghai
```

说明：

- `WEB_ALLOW_REGISTRATION=0`：默认关闭公开注册，更适合公网部署。
- 需要让其他人注册时，临时改成 `1`，注册完成后再改回 `0`。
- 暂时没有 Tavily Key 时，可以保留 `TAVILY_API_KEY=replace-me`，但 Web search 不可用。
- 配置 HTTPS 之前保持 `WEB_COOKIE_SECURE=0`。
- 服务器物理位置不会决定日报时间。按北京时间执行时使用 `Asia/Shanghai`；按韩国时间执行时
  改成 `Asia/Seoul`。
- 默认主模型使用 DeepSeek。要切到小米 MiMo，见下一节。

### 8.1.1 使用小米 MiMo 模型

MiMo 提供 OpenAI 兼容接口。把 `.env` 中模型配置改成：

```env
LLM_PROVIDER=mimo
MIMO_API_KEY=替换为你的小米MiMoKey
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5-pro
LLM_MAX_TOKENS_PARAM=max_completion_tokens
USE_LOCAL_PROXY=0
```

也可以使用通用变量：

```env
LLM_PROVIDER=mimo
LLM_API_KEY=替换为你的小米MiMoKey
LLM_BASE_URL=https://api.xiaomimimo.com/v1
LLM_MODEL=mimo-v2.5-pro
LLM_MAX_TOKENS_PARAM=max_completion_tokens
```

切换模型后重新创建会调用模型的容器：

```bash
sudo docker compose --profile telegram up -d --build --force-recreate \
  agent-console scheduler-worker telegram-worker
```

### 8.1.2 使用多模型 Provider 池

如果你想让普通聊天、代码模式、总结、定时任务分析分别走不同模型，可以同时配置多个
provider。下面示例使用 OpenAI 兼容的自建 API 中转站：

```env
LLM_PROVIDER=openai_relay
OPENAI_RELAY_API_KEY=替换为你的中转站Key
OPENAI_RELAY_BASE_URL=http://43.133.81.4
OPENAI_RELAY_MODEL=gpt-4o-mini
OPENAI_RELAY_MAX_TOKENS_PARAM=max_tokens
OPENAI_RELAY_WIRE_API=responses

LLM_ROUTE_DEFAULT=openai_relay
LLM_ROUTE_CHAT=openai_relay
LLM_ROUTE_CODING=openai_relay
LLM_ROUTE_HYBRID=openai_relay
LLM_ROUTE_SUMMARY=openai_relay
LLM_ROUTE_COMPACT=openai_relay
LLM_ROUTE_SCHEDULER_PLAN=openai_relay
LLM_ROUTE_SCHEDULER_ANALYZE=openai_relay
LLM_ROUTE_TASK_CONCLUSION=openai_relay
```

如果中转站给出的配置是 `wire_api = "responses"`，按它给的 `base_url` 填写，
通常不要手动追加 `/v1`。如果中转站使用普通 Chat Completions 协议，再把
`OPENAI_RELAY_WIRE_API` 改成 `chat_completions`，并按中转站要求填写 `/v1` 地址。
完整说明见 [MODEL_PROVIDER_POOL_ROUTING.md](../runtime/MODEL_PROVIDER_POOL_ROUTING.md)。

不要同时保留旧的：

```env
WEB_USERNAME=
WEB_PASSWORD=
```

使用 `WEB_USERS_JSON` 后，可以删除这两行，避免运维时误判。

### 8.2 Telegram 配置

不需要 Telegram 时，可以跳过本节、第 9 节、第 10.2 节和第 11 节，后续运维命令中也可以省略
`--profile telegram`。

在 Telegram 中打开 `@BotFather`，发送：

```text
/newbot
```

按提示创建机器人，将 Token 写入 `.env`：

```env
TELEGRAM_BOT_TOKEN=替换为BotFather提供的Token
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_USER_MAP=
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_RETRY_DELAY=3
TELEGRAM_NOTIFY_CHAT_IDS=
TELEGRAM_NOTIFY_MAX_CHARS=3500
TELEGRAM_NOTIFY_SEND_REPORT_FILE=1
TELEGRAM_NOTIFY_DOCUMENT_MAX_BYTES=10485760
TELEGRAM_OUTBOX_BATCH_SIZE=10
TELEGRAM_OUTBOX_MAX_ATTEMPTS=3
TELEGRAM_STORAGE_PREVIEW_BYTES=8000
TELEGRAM_STORAGE_DOWNLOAD_MAX_BYTES=10485760
TELEGRAM_PROXY_URL=
```

首尔服务器先保持：

```env
TELEGRAM_PROXY_URL=
```

保存后限制 `.env` 权限：

```bash
chmod 600 .env
```

## 9. 验证 Telegram API 连通性

先从 `.env` 读取 Token：

```bash
cd ~/apps/taleclaw
BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env)"
```

检查 Token 和首尔服务器到 Telegram 的网络：

```bash
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
```

正常结果包含：

```json
{"ok":true}
```

清除可能残留的 webhook，确保可以使用长轮询：

```bash
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/deleteWebhook"
```

如果 `getMe` 超时，先看本文“Telegram 连接失败”章节。首尔节点通常不需要代理。

## 10. 构建和启动

### 10.1 启动 Web 和 scheduler

```bash
cd ~/apps/taleclaw
sudo docker compose up -d --build agent-console scheduler-worker
```

检查容器：

```bash
sudo docker compose ps
sudo docker compose logs --tail=100 agent-console
sudo docker compose logs --tail=100 scheduler-worker
```

检查后端健康：

```bash
curl -u admin:'你的管理员密码' \
  http://127.0.0.1:8000/api/health
```

正常结果包含：

```json
{"ok":true}
```

### 10.2 启动 Telegram Gateway

```bash
sudo docker compose --profile telegram up -d --build telegram-worker
sudo docker compose logs -f --tail=100 telegram-worker
```

日志出现：

```text
taleclaw Telegram gateway started.
```

表示 worker 已启动。按 `Ctrl+C` 只会退出日志查看，不会停止容器。

## 11. 获取 Telegram user ID 并授权

如果 `.env` 中暂时保留：

```env
TELEGRAM_ALLOWED_USER_IDS=
```

向你的 Telegram Bot 发送任意文字。Bot 会回复未授权提示，并返回你的 Telegram user ID。

编辑 `.env`：

```bash
nano .env
```

仅作为普通隔离用户使用：

```env
TELEGRAM_ALLOWED_USER_IDS=123456789
```

如果你希望自己的 Telegram 账号映射到管理员，使用：

```env
TELEGRAM_ALLOWED_USER_IDS=
TELEGRAM_USER_MAP={"123456789":{"user_id":"admin","role":"admin"}}
```

管理员 Telegram 可以使用 Coding、`bash` 和 scheduler 等高权限能力。不要给不受信任的账号授予
`admin`。

重新创建 Telegram 容器，使新 `.env` 生效：

```bash
sudo docker compose --profile telegram up -d --force-recreate telegram-worker
sudo docker compose logs -f --tail=100 telegram-worker
```

向 Bot 发送：

```text
/start
/status
/files
你好
```

### 11.1 定时任务完成后推送到 Telegram

如果定时任务已经在后台完成，但 Telegram 没收到消息，通常是因为只启动了 scheduler，
没有配置通知目标，或者 `scheduler-worker` 和 `telegram-worker` 没有共享 `.gateway` outbox。
当前版本使用：

```text
scheduler-worker -> .gateway/telegram.db outbox -> telegram-worker -> Telegram
```

默认会推送两条：

```text
1. 定时任务摘要
2. 生成的 Markdown 报告文件
```

这对到点自动执行和聊天中“立即运行一次当前任务”都生效。

编辑 `.env`，显式写入你的 Telegram user ID：

```env
TELEGRAM_NOTIFY_CHAT_IDS=123456789
TELEGRAM_NOTIFY_SEND_REPORT_FILE=1
```

如果留空，系统会尝试从 `TELEGRAM_USER_MAP` 的 key 和 `TELEGRAM_ALLOWED_USER_IDS` 推导通知目标；
但 `TELEGRAM_ALLOWED_USER_IDS=*` 不会被当作通知目标。公网部署建议显式填写
`TELEGRAM_NOTIFY_CHAT_IDS`。

让配置生效并同时重建两个 worker：

```bash
sudo docker compose --profile telegram up -d --build --force-recreate \
  scheduler-worker telegram-worker
```

查看日志：

```bash
sudo docker compose logs -f --tail=100 scheduler-worker
sudo docker compose logs -f --tail=100 telegram-worker
```

不想自动发送报告文件时，改成：

```env
TELEGRAM_NOTIFY_SEND_REPORT_FILE=0
```

然后重新创建：

```bash
sudo docker compose --profile telegram up -d --force-recreate \
  scheduler-worker telegram-worker
```

### 11.2 在 Telegram 查看 storage 文件

Telegram 账号映射到哪个 taleclaw 用户，就只能查看该用户自己的：

```text
.users/<user-id>/storage/
```

可用命令：

```text
/files              列出根目录
/files reports      列出 reports 目录
/cat reports/a.md    预览文本文件
/download a.pdf      下载文件
```

如果你把 Telegram 映射为管理员：

```env
TELEGRAM_USER_MAP={"123456789":{"user_id":"admin","role":"admin"}}
```

那么 `/files` 看到的是：

```text
.users/admin/storage/
```

不是仓库根目录的旧版 `storage/`。如果需要把旧文件迁移给管理员：

```bash
mkdir -p .users/admin/storage
cp -a storage/. .users/admin/storage/
```

## 12. 配置 Nginx

### 12.1 暂时没有域名

删除 Ubuntu 默认站点，避免 `server_name _` 冲突：

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

创建配置：

```bash
sudo nano /etc/nginx/sites-available/taleclaw
```

写入：

```nginx
server {
    listen 80 default_server;
    listen [::]:80 default_server;
    server_name _;

    client_max_body_size 55m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 300s;
    }
}
```

启用：

```bash
sudo ln -s /etc/nginx/sites-available/taleclaw \
  /etc/nginx/sites-enabled/taleclaw
sudo nginx -t
sudo systemctl reload nginx
```

如果软链接已存在，使用：

```bash
sudo ln -sf /etc/nginx/sites-available/taleclaw \
  /etc/nginx/sites-enabled/taleclaw
```

访问：

```text
http://你的公网IP/
```

### 12.2 已有域名

先在 DNS 服务商中添加：

```text
A    bot.example.com    你的公网IP
```

编辑 Nginx：

```bash
sudo nano /etc/nginx/sites-available/taleclaw
```

将：

```nginx
server_name _;
```

改为：

```nginx
server_name bot.example.com;
```

检查并重载：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

先确认 HTTP 可访问：

```text
http://bot.example.com/
```

## 13. 配置 HTTPS

只有域名正确解析到当前服务器，并且公网可以访问 `80/tcp` 后，才执行本节。

安装 Certbot：

```bash
sudo snap install --classic certbot
sudo ln -sf /snap/bin/certbot /usr/local/bin/certbot
```

申请证书并让 Certbot 自动修改 Nginx：

```bash
sudo certbot --nginx -d bot.example.com
```

验证自动续期：

```bash
sudo certbot renew --dry-run
```

编辑项目 `.env`：

```bash
cd ~/apps/taleclaw
nano .env
```

修改：

```env
WEB_COOKIE_SECURE=1
```

重新创建 Web 容器：

```bash
sudo docker compose up -d --force-recreate agent-console
```

访问：

```text
https://bot.example.com/
```

## 14. 常用运维命令

进入项目目录：

```bash
cd ~/apps/taleclaw
```

查看容器：

```bash
sudo docker compose --profile telegram ps
```

查看日志：

```bash
sudo docker compose logs -f --tail=200 agent-console
sudo docker compose logs -f --tail=200 scheduler-worker
sudo docker compose logs -f --tail=200 telegram-worker
```

重启：

```bash
sudo docker compose --profile telegram restart
```

停止，不删除持久化数据：

```bash
sudo docker compose --profile telegram down
```

重新启动：

```bash
sudo docker compose --profile telegram up -d
```

## 15. 更新代码

服务器只负责运行，不建议直接修改服务器工作区代码。

```bash
cd ~/apps/taleclaw
git status --short
git pull --ff-only
sudo docker compose --profile telegram up -d --build --force-recreate
sudo docker compose --profile telegram ps
```

如果 `git status --short` 有输出，先确认服务器上的本地改动是否需要备份。不要直接运行破坏性命令。

## 16. 数据目录和备份

重要数据：

```text
.env                       密钥和部署配置
.users/                    Web 用户、用户私有记忆和 storage
.sessions/                 会话数据库
.scheduler/                定时任务数据库
.gateway/                  Telegram offset、当前会话和 scheduler 通知 outbox
.task_sessions/            TaskSession 日志
.task_sandbox/             任务临时目录
storage/                   旧版或管理员全局 storage
memory/                    旧版或管理员全局 memory
```

一致性要求较高时，先停止容器再备份：

```bash
cd ~/apps/taleclaw
sudo docker compose --profile telegram down
mkdir -p ~/backups
tar -czf ~/backups/taleclaw-$(date +%F-%H%M%S).tar.gz \
  .env \
  .users \
  .sessions \
  .scheduler \
  .gateway \
  .task_sessions \
  .task_sandbox \
  storage \
  memory
sudo docker compose --profile telegram up -d
```

如果部分目录尚未生成，`tar` 会提示目录不存在。可以从命令中删除对应目录后再执行。

## 17. 故障排查

### 17.1 `docker compose` 找不到配置文件

现象：

```text
no configuration file provided: not found
```

处理：

```bash
cd ~/apps/taleclaw
ls -la docker-compose.yml Dockerfile .env
```

### 17.2 Docker daemon 没有运行

现象：

```text
Cannot connect to the Docker daemon
```

处理：

```bash
sudo systemctl enable --now docker
sudo systemctl status docker --no-pager
sudo docker ps
```

### 17.3 Docker Hub 或 pip 下载超时

首尔节点通常比中国大陆节点稳定。先重试：

```bash
sudo docker compose --profile telegram build
sudo docker compose --profile telegram up -d
```

仍失败时，检查服务器出站网络、DNS 和云厂商防火墙。

### 17.4 Nginx 返回 `502` 或 `503`

先检查后端：

```bash
cd ~/apps/taleclaw
sudo docker compose ps
sudo docker compose logs --tail=200 agent-console
curl -u admin:'你的管理员密码' \
  http://127.0.0.1:8000/api/health
sudo nginx -t
sudo journalctl -u nginx -n 100 --no-pager
```

### 17.5 页面没有注册按钮

编辑真实 `.env`：

```env
WEB_ALLOW_REGISTRATION=1
```

重新创建 Web 容器：

```bash
sudo docker compose up -d --force-recreate agent-console
```

检查：

```bash
curl http://127.0.0.1:8000/api/auth/config
```

正常结果：

```json
{"registration_enabled":true}
```

### 17.6 Telegram 连接失败

宿主机检查：

```bash
cd ~/apps/taleclaw
BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env)"
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
```

常见日志：

| 日志 | 原因和处理 |
| --- | --- |
| `could not connect` 或 `connection timed out` | 无法访问 Telegram API |
| `proxy connection failed` | 代理地址无法从容器访问 |
| `HTTP 401` 或 `HTTP 404` | Bot Token 错误 |
| `HTTP 409` | 残留 webhook，或另一个 worker 正在轮询 |
| `HTTP 429` | Telegram 限流，等待后重试 |

首尔节点实测仍无法直连时，再配置：

```env
TELEGRAM_PROXY_URL=http://host.docker.internal:7890
```

然后重新创建：

```bash
sudo docker compose --profile telegram up -d --force-recreate telegram-worker
```

### 17.7 定时任务完成但 Telegram 没收到

先确认通知目标：

```bash
cd ~/apps/taleclaw
grep -E '^(TELEGRAM_NOTIFY_CHAT_IDS|TELEGRAM_ALLOWED_USER_IDS|TELEGRAM_USER_MAP)=' .env
```

自用最简单配置：

```env
TELEGRAM_NOTIFY_CHAT_IDS=你的TelegramUserID
```

确认两个 worker 都在：

```bash
sudo docker compose --profile telegram ps
sudo docker compose logs --tail=100 scheduler-worker
sudo docker compose logs --tail=100 telegram-worker
```

检查 outbox 是否有待发送消息：

```bash
sudo docker compose --profile telegram exec -T telegram-worker \
  python - <<'PY'
from gateway.telegram.store import TelegramGatewayStore
store = TelegramGatewayStore()
print(store.list_pending_messages(limit=20))
store.close()
PY
```

如果 outbox 有内容但一直没发出，优先看 `telegram-worker` 日志和 Telegram 网络连通性。
如果 outbox 没内容，说明 scheduler 没找到通知目标，或者定时任务还没有真正执行。

修改 `.env` 或更新代码后，同时重建：

```bash
sudo docker compose --profile telegram up -d --build --force-recreate \
  scheduler-worker telegram-worker
```

### 17.8 Git 拉取失败或分叉

先查看：

```bash
cd ~/apps/taleclaw
git status --short
git branch --show-current
git log --oneline --decorate -n 5
```

服务器不应直接写业务代码。没有需要保留的服务器本地提交时，优先保持服务器工作区只跟随远端。
遇到分叉时先备份并确认本地提交用途，再决定 merge 或 rebase。

### 17.9 临时把首尔服务器当 Telegram 代理

先分清两个场景：

- 只是为了在本地 Telegram 客户端里访问 `@BotFather` 创建 Bot：推荐 SSH SOCKS 隧道。
- 要让另一台国内服务器调用 Telegram Bot API：推荐临时 Squid HTTP 代理，并且必须加认证和来源 IP 限制。

taleclaw 需要的是 BotFather 提供的 Bot Token，不需要申请 `my.telegram.org` 上的 API ID
和 API Hash。

#### 方案 A：SSH SOCKS 隧道，本地申请 Bot Token

这是最安全的临时方式，不需要在首尔服务器开放新端口。

在你的本地电脑执行：

```bash
ssh -N -D 127.0.0.1:1080 ubuntu@你的首尔服务器公网IP
```

保持这个终端不要关闭。

然后在 Telegram Desktop 中配置代理：

```text
Proxy type: SOCKS5
Server: 127.0.0.1
Port: 1080
Username: 留空
Password: 留空
```

连接成功后，打开 `@BotFather`：

```text
/newbot
```

创建 Bot 后得到 `TELEGRAM_BOT_TOKEN`。将它写入首尔服务器的 `.env`：

```bash
cd ~/apps/taleclaw
nano .env
```

测试：

```bash
BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env)"
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
```

#### 方案 B：Squid HTTP 代理，给另一台服务器调用 Bot API

只有需要让国内服务器借用首尔服务器访问 Telegram API 时，才使用这个方案。

在首尔服务器安装：

```bash
sudo apt-get update
sudo apt-get install -y squid apache2-utils
```

创建代理账号。密码建议只用字母、数字、短横线和下划线，避免 URL 编码问题：

```bash
sudo htpasswd -c /etc/squid/passwd taleproxy
```

备份默认配置：

```bash
sudo cp /etc/squid/squid.conf /etc/squid/squid.conf.bak.$(date +%F-%H%M%S)
```

将 `CLIENT_IP` 替换为需要使用代理的那台服务器公网 IP：

```bash
CLIENT_IP=国内服务器公网IP
sudo tee /etc/squid/squid.conf >/dev/null <<EOF
acl allowed_client src ${CLIENT_IP}/32
acl SSL_ports port 443
acl Safe_ports port 443
acl CONNECT method CONNECT

auth_param basic program /usr/lib/squid/basic_ncsa_auth /etc/squid/passwd
auth_param basic realm taleclaw-telegram-proxy
acl authenticated proxy_auth REQUIRED

http_access deny !Safe_ports
http_access deny CONNECT !SSL_ports
http_access allow allowed_client authenticated
http_access deny all

http_port 3128
access_log /var/log/squid/access.log
EOF
```

重启并检查：

```bash
sudo systemctl enable --now squid
sudo systemctl restart squid
sudo systemctl status squid --no-pager
```

在云厂商安全组中开放：

```text
3128/tcp  只允许 国内服务器公网IP
```

如果服务器启用了 UFW：

```bash
sudo ufw allow from 国内服务器公网IP to any port 3128 proto tcp
```

在国内服务器测试：

```bash
BOT_TOKEN="你的BotToken"
curl -x http://taleproxy:你的代理密码@首尔服务器公网IP:3128 \
  --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/getMe"
```

成功后，在国内服务器的 taleclaw `.env` 中配置：

```env
TELEGRAM_PROXY_URL=http://taleproxy:你的代理密码@首尔服务器公网IP:3128
```

重启 Telegram worker：

```bash
sudo docker compose --profile telegram up -d --force-recreate telegram-worker
```

如果代理密码包含 `@`、`:`、`#`、`/` 等特殊字符，需要先 URL encode，或者改成只含
字母、数字、短横线和下划线的临时密码。

用完后建议关闭临时代理：

```bash
sudo systemctl stop squid
sudo systemctl disable squid
```

确认安全组移除 `3128/tcp`。不要长期开放没有来源 IP 限制的代理，否则很容易变成公开代理被滥用。

## 18. 最终验收清单

```bash
cd ~/apps/taleclaw

sudo docker compose --profile telegram ps

curl -u admin:'你的管理员密码' \
  http://127.0.0.1:8000/api/health

curl http://127.0.0.1:8000/api/auth/config

sudo nginx -t

BOT_TOKEN="$(sed -n 's/^TELEGRAM_BOT_TOKEN=//p' .env)"
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${BOT_TOKEN}/getMe"

sudo docker compose logs --tail=100 telegram-worker
```

浏览器检查：

```text
http://公网IP/
```

或：

```text
https://你的域名/
```

Telegram 检查：

```text
/start
/status
你好
```

## 19. 官方参考

- Docker Engine on Ubuntu: <https://docs.docker.com/engine/install/ubuntu/>
- Docker Compose plugin: <https://docs.docker.com/compose/install/linux/>
- Nginx reverse proxy: <https://docs.nginx.com/nginx/admin-guide/web-server/reverse-proxy>
- Certbot Nginx instructions: <https://certbot.eff.org/instructions?ws=nginx&os=snap>
- Telegram Bot API: <https://core.telegram.org/bots/api>
- Telegram Bots FAQ: <https://core.telegram.org/bots/faq>
- Ubuntu Squid proxy overview: <https://ubuntu.com/server/docs/explanation/web-services/about-squid-proxy-servers/>
- Squid authentication: <https://wiki.squid-cache.org/Features/Authentication>
