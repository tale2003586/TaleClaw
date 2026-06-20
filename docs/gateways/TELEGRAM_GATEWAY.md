# Telegram Gateway

新服务器从零部署见：[首尔服务器完整部署手册](../deployment/SEOUL_SERVER_DEPLOYMENT.md)。

## 目标

taleclaw 现在可以通过 Telegram Bot 接收私聊文字消息，并把消息交给现有 AgentLoop。
Telegram 只是一个渠道适配器，不会侵入 Pipeline、工具插件或记忆系统。

```text
Telegram Bot API
      |
telegram-worker
      |
TelegramGateway
      |
InboundMessage -> MessageBus -> AgentLoop -> OutboundMessage
```

## 第一阶段范围

当前已支持：

- Telegram Bot API `getUpdates` 长轮询
- 私聊文字消息
- 隔离用户身份
- 用户白名单
- 显式账号映射和管理员角色
- `/start`
- `/new`
- `/status`
- `/files`、`/cat`、`/download` 查看当前用户的私有 storage
- scheduler 定时任务完成后主动推送摘要和报告文件到 Telegram
- AI 回复超长文本自动拆分
- 长轮询 offset 持久化
- Telegram outbox 持久化投递和失败重试
- Docker Compose 按需启动

当前没有开放：

- 群聊
- 接收 Telegram 图片、语音和文件
- Web 页面中的 Telegram 绑定验证码
- webhook

这些能力可以在现有 Gateway 层继续增加，不需要重写 AgentLoop。

## 代码结构

```text
gateway/base.py                 通用 ChannelAdapter 接口
gateway/telegram/client.py      Telegram Bot API HTTP 客户端
gateway/telegram/identity.py    Telegram 账号白名单和 taleclaw 用户映射
gateway/telegram/store.py       offset、当前会话和 outbox PostgreSQL 存储
gateway/telegram/storage.py     Telegram storage 浏览、预览和下载
gateway/telegram/adapter.py     Telegram Update 与内部消息互转
telegram_worker.py              独立 worker 入口
scheduler_worker.py             定时任务执行，并把通知写入 Telegram outbox
```

运行状态保存在 PostgreSQL：

```text
TELEGRAM_DATABASE_URL -> GATEWAY_DATABASE_URL -> DATABASE_URL
```

## 创建 Bot

在 Telegram 中打开 `@BotFather`：

```text
/newbot
```

按照提示创建 Bot，保存 Bot Token。Token 等同于机器人密码，不要提交到 Git。

## 环境变量

在服务器项目目录编辑真实 `.env`：

```bash
nano .env
```

最小自用配置：

```env
TELEGRAM_BOT_TOKEN=替换为BotFather提供的Token
TELEGRAM_ALLOWED_USER_IDS=123456789
```

首次不知道自己的 Telegram user ID 时，可以先启动 worker，向 Bot 发送任意文字。未授权提示
会返回当前 Telegram user ID。把该数字写入 `.env` 后重启 worker。

### 映射已有账号

普通映射：

```env
TELEGRAM_USER_MAP={"123456789":{"user_id":"alice","role":"user"}}
```

自用管理员映射：

```env
TELEGRAM_USER_MAP={"123456789":{"user_id":"admin","role":"admin"}}
```

映射后的 Telegram 消息会使用对应账号自己的：

```text
.users/<user-id>/storage/
.users/<user-id>/memory/
```

只有显式配置 `"role":"admin"` 才会获得 Coding、`bash` 和 scheduler 等高权限。

### 开放普通账号

```env
TELEGRAM_ALLOWED_USER_IDS=*
```

这会允许任意 Telegram 用户使用 Bot，但每个人只会获得独立的普通账号：

```text
telegram_<Telegram user ID>
```

公网 Bot 建议优先使用白名单，不建议一开始就配置 `*`。

### 其他配置

```env
TELEGRAM_POLL_TIMEOUT=30
TELEGRAM_RETRY_DELAY=3
TELEGRAM_NOTIFY_CHAT_IDS=123456789
TELEGRAM_NOTIFY_MAX_CHARS=3500
TELEGRAM_NOTIFY_SEND_REPORT_FILE=1
TELEGRAM_NOTIFY_DOCUMENT_MAX_BYTES=10485760
TELEGRAM_OUTBOX_BATCH_SIZE=10
TELEGRAM_OUTBOX_MAX_ATTEMPTS=3
TELEGRAM_STORAGE_PREVIEW_BYTES=8000
TELEGRAM_STORAGE_DOWNLOAD_MAX_BYTES=10485760
TELEGRAM_PROXY_URL=
```

| 变量 | 含义 |
| --- | --- |
| `TELEGRAM_POLL_TIMEOUT` | `getUpdates` 长轮询等待秒数 |
| `TELEGRAM_RETRY_DELAY` | 网络异常后的重试等待秒数 |
| `TELEGRAM_NOTIFY_CHAT_IDS` | 定时任务完成后要推送到哪些 Telegram chat ID，逗号分隔 |
| `TELEGRAM_NOTIFY_MAX_CHARS` | 定时任务报告推送时最多附带多少字符 |
| `TELEGRAM_NOTIFY_SEND_REPORT_FILE` | 定时任务完成后是否把报告文件作为 Telegram 文件发送，默认 `1` |
| `TELEGRAM_NOTIFY_DOCUMENT_MAX_BYTES` | 自动推送报告文件的最大大小 |
| `TELEGRAM_OUTBOX_BATCH_SIZE` | Telegram worker 每轮最多投递多少条 outbox 消息 |
| `TELEGRAM_OUTBOX_MAX_ATTEMPTS` | outbox 单条消息最多失败重试次数 |
| `TELEGRAM_STORAGE_PREVIEW_BYTES` | `/cat` 单次最多预览多少字节 |
| `TELEGRAM_STORAGE_DOWNLOAD_MAX_BYTES` | `/download` 允许下载的最大文件大小 |
| `TELEGRAM_PROXY_URL` | 可选的 Telegram HTTP 代理，例如 `http://127.0.0.1:7890` |

`TELEGRAM_NOTIFY_CHAT_IDS` 留空时，scheduler 会尝试从 `TELEGRAM_USER_MAP` 的 key 和
`TELEGRAM_ALLOWED_USER_IDS` 中推导通知目标；`*` 不会被当作通知目标。自用部署建议显式写：

```env
TELEGRAM_NOTIFY_CHAT_IDS=你的TelegramUserID
```

中国大陆服务器通常无法直接访问 Telegram API。如果日志持续出现网络错误，需要为服务器提供
可用的 HTTP 代理，并设置 `TELEGRAM_PROXY_URL`。Docker 容器中的 `127.0.0.1` 指向容器自身。
Compose 已将 `host.docker.internal` 映射到宿主机网关；代理运行在宿主机并允许 Docker 容器访问
时，可以配置：

```env
TELEGRAM_PROXY_URL=http://host.docker.internal:7890
```

## Docker 启动

`telegram-worker` 使用 Compose profile，默认不会跟随旧部署自动启动。配置 `.env` 后执行：

```bash
sudo docker compose --profile telegram up -d --build telegram-worker
```

查看日志：

```bash
sudo docker compose logs -f --tail=200 telegram-worker
```

停止 Telegram 接入：

```bash
sudo docker compose --profile telegram stop telegram-worker
```

更新代码后重新创建：

```bash
git pull
sudo docker compose --profile telegram up -d --build --force-recreate scheduler-worker telegram-worker
```

如果只改了 Telegram Token、白名单、通知目标等 `.env` 配置，不需要重新 build：

```bash
sudo docker compose --profile telegram up -d --force-recreate scheduler-worker telegram-worker
```

## 定时任务主动推送

scheduler 不直接调用 Telegram API，而是把通知写入共享数据库：

```text
TELEGRAM_DATABASE_URL -> GATEWAY_DATABASE_URL -> DATABASE_URL
```

`telegram-worker` 会在轮询消息前后读取 outbox 并投递。定时任务完成后会先发送文字摘要，再把
报告文件作为 Telegram document 发送。这样即使 Telegram 网络临时失败，消息和文件也会保留在
outbox 中等待重试。

这套通知同时适用于两种执行入口：

- 到点后由 `scheduler-worker` 自动执行
- 聊天中调用 `schedule_run_now` 立即执行一次

要让定时任务完成后主动发给你，至少确认三件事：

```env
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ALLOWED_USER_IDS=你的TelegramUserID
TELEGRAM_NOTIFY_CHAT_IDS=你的TelegramUserID
TELEGRAM_NOTIFY_SEND_REPORT_FILE=1
```

或者使用管理员映射：

```env
TELEGRAM_USER_MAP={"123456789":{"user_id":"admin","role":"admin"}}
TELEGRAM_NOTIFY_CHAT_IDS=123456789
```

Compose 已为 `scheduler-worker` 和 `telegram-worker` 挂载同一个 `.gateway` 目录。更新后需要同时
重建这两个容器：

```bash
sudo docker compose --profile telegram up -d --build --force-recreate scheduler-worker telegram-worker
```

不想自动发送报告文件时，可以关闭：

```env
TELEGRAM_NOTIFY_SEND_REPORT_FILE=0
```

检查 outbox：

```bash
sudo docker compose --profile telegram exec -T telegram-worker \
  python - <<'PY'
from gateway.telegram.store import TelegramGatewayStore
store = TelegramGatewayStore()
print(store.list_pending_messages(limit=20))
store.close()
PY
```

## 排查连接失败

看到以下日志时：

```text
Telegram Bot API request failed for getUpdates
```

先确认容器拿到了 Token：

```bash
sudo docker compose --profile telegram exec -T telegram-worker \
  sh -lc 'test -n "$TELEGRAM_BOT_TOKEN" && echo token-set || echo token-missing'
```

在服务器宿主机验证 Token 和 Telegram API 连通性：

```bash
set -a
source .env
set +a
curl --connect-timeout 10 --max-time 20 \
  "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe"
```

常见错误：

| 日志 | 原因和处理方式 |
| --- | --- |
| `could not connect` 或 `connection timed out` | 服务器无法访问 `api.telegram.org`，配置 `TELEGRAM_PROXY_URL` |
| `proxy connection failed` | 容器无法访问配置的代理地址 |
| `HTTP 401` 或 `HTTP 404` | `TELEGRAM_BOT_TOKEN` 错误，回到 `@BotFather` 核对 |
| `HTTP 409` | 已配置 webhook，或另一个实例也在调用 `getUpdates` |
| `HTTP 429` | Telegram 限流，等待后重试 |

如果此前设置过 webhook，现在改用长轮询，可清除 webhook：

```bash
curl "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook"
```

`getUpdates` 与 webhook 不能同时使用。

## 不使用 Docker

```bash
python telegram_worker.py
```

## Bot 命令

```text
/start              显示接入状态和命令
/help               显示 storage 命令帮助
/new                创建一个新的 Telegram 对话
/status             显示当前绑定用户、角色和会话编号
/files [目录]        列出当前用户私有 storage 中的文件
/ls [目录]           /files 的别名
/cat <文件>          预览文本文件
/download <文件>     下载文件
```

直接发送普通文字时，TelegramGateway 会构造：

```text
InboundMessage(
  channel="telegram",
  chat_id="tg_<chat-id>_<conversation-id>_<user-id>",
  metadata={
    "user_id": "...",
    "user_role": "...",
    "gateway": "telegram"
  }
)
```

因此 Web、Telegram 和 CLI 会话互不混淆，但映射到同一个 taleclaw 用户时可以使用同一套隔离记忆
和 storage。

storage 命令只访问当前 Telegram 身份映射到的用户目录：

```text
.users/<user-id>/storage/
```

例如：

```text
/files
/files reports
/cat reports/daily.md
/download generated/report.pdf
```

## 安全边界

- 默认拒绝未授权账号。
- 当前只处理 `chat.type=private` 的消息。
- 群聊不会进入 AgentLoop，避免多人共享会话时错误继承权限。
- Bot Token 只放在 `.env`。
- 管理员权限必须在 `TELEGRAM_USER_MAP` 中显式授予。
- Telegram API 调用异常不会在错误信息中打印 Bot Token。
- storage 命令只允许访问当前用户自己的 `.users/<user-id>/storage/`，并拒绝路径逃逸和符号链接。
- `/download` 默认限制为 10 MB，可通过 `TELEGRAM_STORAGE_DOWNLOAD_MAX_BYTES` 调整。

## 后续方向

### Webhook

有 HTTPS 域名后，可以增加：

```text
POST /gateway/telegram/webhook
```

并使用 Telegram 的 `secret_token` 校验请求来源。长轮询和 webhook 应二选一。

### scheduler 主动推送扩展

```text
scheduler-worker -> outbox -> telegram-worker -> sendMessage/sendDocument
```

当前已经使用持久化 outbox，不依赖进程内 MessageBus 跨容器直接发送。后续可以继续增加：

- Web 页面查看 outbox 投递状态
- 对单个定时任务配置不同的通知目标
- 失败消息的手动重发入口

### Web 绑定

多用户公开部署时，可以在 Web 端生成一次性验证码：

```text
/bind ABCD-1234
```

由 Gateway 将 Telegram 账号与已经登录的 Web 账号绑定。

## 验证

```bash
python -m unittest discover -s tests -p 'test_telegram_gateway.py' -v
python -m py_compile telegram_worker.py gateway/base.py gateway/telegram/*.py
docker compose config --quiet
git diff --check
```

本次改动完成后，完整单元测试结果为：

```text
Ran 133 tests
OK
```

## 官方参考

- [`getUpdates`](https://core.telegram.org/bots/api#getupdates)：长轮询、`offset` 和 `timeout`
- [`sendMessage`](https://core.telegram.org/bots/api#sendmessage)：发送文本消息与长度限制
- [`sendDocument`](https://core.telegram.org/bots/api#senddocument)：发送 storage 文件
- [`setWebhook`](https://core.telegram.org/bots/api#setwebhook)：后续 webhook 与 `secret_token`
