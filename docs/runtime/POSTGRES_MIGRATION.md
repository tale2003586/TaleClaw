# PostgreSQL 运行时存储说明

当前运行态关系型存储只支持 PostgreSQL。`DATABASE_URL` 是最低必填项；如果某个子系统需要独立库或独立 schema，可以设置更具体的 `*_DATABASE_URL` 覆盖。

## 覆盖范围

以下数据进入 PostgreSQL：

- 会话与消息。
- Web 登录账号与浏览器 session。
- 被上下文 recent window 淘汰后的 memory archive。
- Telegram / Feishu 网关状态、会话映射和 outbox。
- Trace run 索引和 execution path 摘要。

以下数据仍是本地文件工件：

- `memory/*.md`、`memory/PENDING.json` 等人工可读记忆文件。
- `.runs/<run_id>/` 下的原始 trace、report、metrics 和 workspace diff。
- `.evals/` 下的评测结果。
- Qdrant 向量库数据。

## 配置

最简单配置：

```env
DATABASE_URL=postgresql://agent:agent_dev_password@127.0.0.1:55432/agent_console
```

按模块覆盖：

```env
SESSION_DATABASE_URL=
WEB_AUTH_DATABASE_URL=
MEMORY_ARCHIVE_DATABASE_URL=
GATEWAY_DATABASE_URL=
TELEGRAM_DATABASE_URL=
FEISHU_DATABASE_URL=
TRACE_DATABASE_URL=
```

解析顺序是：

1. 调用方显式传入的 PostgreSQL DSN。
2. 子系统专用环境变量。
3. `DATABASE_URL`。

如果没有可用 PostgreSQL DSN，store 会直接报错，不会创建本地关系型数据库文件。

## 本地启动

```bash
docker compose up -d postgres
python -c "import psycopg; print(psycopg.__version__)"
```

默认端口映射：

```env
POSTGRES_HOST_PORT=55432
```
