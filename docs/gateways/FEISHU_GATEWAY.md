# Feishu Gateway

本项目已接入第一版飞书网关，目标是让飞书里的私聊/群聊消息进入 taleclaw 的同一套 Agent Runtime，并支持定时任务报告推送。

当前能力：

- 飞书自建应用机器人接收文字消息。
- 复用 `AgentLoop`、`Pipeline`、工具系统和多用户记忆隔离。
- 支持 `/new`、`/status`、`/files`、`/cat`、`/download`。
- 支持 scheduler 任务完成后推送文字摘要和报告文件。
- 使用 HTTP 事件回调，不依赖飞书 Python SDK。

暂不支持：

- 加密事件回调。飞书后台请先不要开启事件加密。
- 飞书交互式卡片审批。
- 飞书云文档读写。

## 1. 飞书开放平台配置

1. 进入飞书开放平台，创建企业自建应用。
2. 在应用能力中启用机器人。
3. 配置事件订阅，选择“将事件发送至开发者服务器”。
4. 请求地址填写：

```text
https://你的域名/feishu/events
```

5. 订阅事件：

```text
接收消息 im.message.receive_v1
```

6. 权限建议开通：

```text
以应用身份发送消息
读取用户发送给机器人的消息
上传并发送文件
```

不同飞书后台版本的权限名称可能略有不同，以开放平台页面显示为准。核心原则是：机器人能接收消息、能发消息、能上传文件。

## 2. 环境变量

在 `.env` 中添加：

```bash
FEISHU_APP_ID=cli_xxx
FEISHU_APP_SECRET=xxx
FEISHU_VERIFICATION_TOKEN=xxx
FEISHU_CALLBACK_HOST=0.0.0.0
FEISHU_CALLBACK_PORT=8010
FEISHU_CALLBACK_PATH=/feishu/events

# 只允许指定 open_id 使用；也可以写 * 允许任意飞书用户注册为普通隔离账号。
FEISHU_ALLOWED_OPEN_IDS=ou_xxx

# 推荐显式映射管理员。
# FEISHU_USER_MAP={"ou_xxx":{"user_id":"admin","role":"admin"}}
FEISHU_USER_MAP=

# 群聊默认只有 @ 机器人时响应。设为 1 会回复群里所有文本。
FEISHU_RESPOND_IN_GROUPS=0
FEISHU_BOT_OPEN_ID=

# 定时任务推送目标。发送 /status 后可看到飞书 chat_id。
FEISHU_NOTIFY_CHAT_IDS=oc_xxx
FEISHU_NOTIFY_SEND_REPORT_FILE=1
```

第一次不知道自己的 `open_id` 时，可以先临时设置：

```bash
FEISHU_ALLOWED_OPEN_IDS=*
```

然后在飞书私聊机器人发送 `/status`，确认可用后再改成显式 `FEISHU_USER_MAP`。

## 3. 本地启动

```bash
python feishu_worker.py
```

健康检查：

```bash
curl http://127.0.0.1:8010/health
```

## 4. Docker 启动

```bash
sudo docker compose --profile feishu up -d --build feishu-worker
sudo docker compose logs -f --tail=100 feishu-worker
```

## 5. Nginx 反向代理

把下面的 location 放到你已有的站点配置中：

```nginx
location /feishu/events {
    proxy_pass http://127.0.0.1:8010/feishu/events;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

然后：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

飞书事件回调地址填写：

```text
https://你的域名/feishu/events
```

## 6. 飞书里测试

私聊机器人：

```text
/start
```

查看当前用户、角色、飞书 chat_id：

```text
/status
```

列出 storage：

```text
/files
```

预览文件：

```text
/cat generated/daily-ai-news.md
```

下载文件：

```text
/download generated/daily-ai-news.pdf
```

普通对话：

```text
帮我搜索今天最新的 AI Agent 新闻，并生成一个简短总结。
```

## 7. 定时任务推送

1. 先在飞书发送 `/status`，复制返回里的 `飞书 chat_id`。
2. 写入 `.env`：

```bash
FEISHU_NOTIFY_CHAT_IDS=oc_xxx
FEISHU_NOTIFY_SEND_REPORT_FILE=1
```

3. 重启 worker：

```bash
sudo docker compose --profile feishu up -d --force-recreate feishu-worker
sudo docker compose up -d --force-recreate scheduler-worker
```

之后 scheduler 完成任务时，会写入 PostgreSQL outbox，`feishu-worker` 会轮询 outbox 并推送到飞书。

## 8. 当前代码结构

```text
feishu_worker.py
gateway/feishu/
  adapter.py   HTTP 回调、命令处理、runtime 投递、outbox 发送
  client.py    tenant_access_token、发送消息、上传文件
  identity.py  open_id 到 taleclaw user_id/role 的映射
  store.py     PostgreSQL 事件去重、会话映射、outbox
```

内部消息链路：

```text
Feishu callback
  -> FeishuGateway.handle_callback()
  -> runtime.submit_user_message(channel="feishu")
  -> AgentLoop / Pipeline / Tools / Memory
  -> MessageBus outbound
  -> FeishuGateway.send()
  -> Feishu send message API
```
