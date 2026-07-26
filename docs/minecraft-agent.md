# Minecraft 资源任务 Agent

该功能让 TaleClaw 通过受限的 Mineflayer Bridge 控制一个 Java 版服务器 Bot，执行“收集 4 个原木”“挖 30 个钻石”一类资源任务。它默认关闭，只支持离线认证，并禁止战斗、命令、容器、爆炸物、原始协议和任意代码动作。

## 启动

服务器需为 Java 版并已开启离线模式。先生成一个随机 Bridge token，写入 `.env`：

```dotenv
MINECRAFT_AGENT_ENABLED=1
MINECRAFT_BRIDGE_TOKEN=replace-with-a-long-random-token
MINECRAFT_SERVER_HOST=host.docker.internal
MINECRAFT_SERVER_PORT=25565
MINECRAFT_BOT_USERNAME=TaleClawBot
MINECRAFT_AUTH_MODE=offline
DATABASE_URL=postgresql://agent:agent_dev_password@postgres:5432/agent_console
MINECRAFT_DATABASE_URL=postgresql://agent:agent_dev_password@postgres:5432/agent_console
```

Bridge 与主程序在宿主机运行：

```bash
cd minecraft-bridge
npm ci
npm start
```

Bridge 使用 Compose 可选 profile 运行：

```bash
docker compose --profile minecraft up -d postgres minecraft-bridge
```

默认 Compose 不启动 Bridge。远程绑定还必须显式配置 `MINECRAFT_BRIDGE_TRUSTED_CLIENTS`；不要把 Bridge 端口直接暴露到公网。

## 下达和管理任务

聊天入口：

```text
/minecraft 收集 4 个原木
/minecraft 挖 30 个钻石
```

CLI：

```bash
python scripts/minecraft_task.py start diamond 30
python scripts/minecraft_task.py status mc_xxx
python scripts/minecraft_task.py cancel mc_xxx
```

配置检查默认不联网。只有显式传入 `--connect` 才连接服务器：

```bash
python scripts/minecraft_smoke.py --check-only
python scripts/minecraft_smoke.py --connect
```

## 持久化与恢复

设置 `MINECRAFT_PERSISTENCE_ENABLED=1` 后使用 `MINECRAFT_DATABASE_URL`，未设置时复用 `DATABASE_URL`。任务、事件、checkpoint、取消请求和 Worker lease 写入 PostgreSQL。启动时会先连接并观察，再核对 Bot、服务器、世界、背包基线与计划版本；身份不一致的任务会进入 `blocked`，不会猜测或重复宣告完成。

## 常见故障

- `unauthorized`：主程序和 Bridge 的 token 不一致。
- `client_not_allowed`：远程 Bridge 的客户端地址不在 allowlist。
- `insufficient_tool_tier`：目标矿石所需镐等级不满足；钻石至少需要铁镐。
- `resource_not_found`：观察半径内未发现目标，Worker 会有界恢复或重新规划。
- `resume_identity_mismatch`：恢复时服务器、世界、Bot 或基线发生变化，需要人工确认并新建任务。
- `action_budget_exhausted` / `model_budget_exhausted`：任务达到安全预算，不会继续提交动作或模型调用。

离线模式意味着服务器不验证 Mojang/Microsoft 身份。请只在自有或明确授权的服务器上使用，并通过白名单、网络隔离和最小权限保护服务器。
