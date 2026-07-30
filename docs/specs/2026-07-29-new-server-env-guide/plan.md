# 新服务器 `.env` 配置手册 Plan

## 架构概览

本任务产出一份独立部署文档 `docs/deployment/NEW_SERVER_ENV_GUIDE.md`。文档围绕新服务器首次配置 `.env` 的时间顺序组织：准备与备份 → 最小配置模板 → 分组解释 → 安全验证 → 应用配置 → 故障排查。它不复制完整服务器部署流程，而是在开头和结尾链接现有的 `SEOUL_SERVER_DEPLOYMENT.md`。

配置内容以三个当前事实源交叉校验：

1. `.env.example` 提供公开示例、分组方式和可选功能入口；
2. `docker-compose.yml` 决定容器服务、Compose 插值、端口和容器内路径；
3. `config.py` 与运行时模块决定 TaskState、动态预算和 Artifact 外置的真实默认行为。

## 核心文档结构

### 1. 适用范围与前置条件

说明默认系统、部署方式、代码路径，以及手册只处理 `.env`。列出进入项目目录、确认 `.env.example` 存在的只读命令。

### 2. 创建、备份与保护 `.env`

分别覆盖首次创建和已有配置升级：首次从 `.env.example` 复制；已有文件先生成带时间戳的权限受限备份。统一设置 `chmod 600`，并提醒不要提交 Git、不要把文件内容粘贴到日志或聊天中。

### 3. 最小生产配置

提供一个连续、可复制的 dotenv 模板，包含：

- OpenAI 兼容 Provider 和单 Provider 路由；
- PostgreSQL Compose 服务及共用 DSN；
- Web 多用户管理员登录和安全开关；
- 容器内 `/app` 工作区；
- TaskState、语义压缩、Artifact 外置、动态预算和长内容阈值；
- 默认关闭的 RAG、匿名访问和注册。

模板使用明显占位符，避免用户把示例密码误当生产密码。

### 4. 配置分组说明

用“必须修改 / 建议保持 / 按需启用”标签解释各组变量。Provider 说明 `base_url`、model、wire API 和 context window；数据库说明容器内外地址；Web 说明 JSON 格式与 Cookie；工作区说明宿主机和容器路径差异；上下文说明新权威配置和 Artifact 持久化位置。

### 5. 无秘密泄露的验证流程

验证分为四层：

1. 文件存在性、权限和 Git 忽略状态；
2. `docker compose config --quiet` 验证 Compose 解析，不输出展开后的秘密；
3. 启动/重建服务并检查 `docker compose ps` 和有限日志；
4. 在容器内只检查变量是否存在或输出非敏感开关，不打印 key、密码或 DSN。

数据库使用 `pg_isready` 验证，不回显连接串。模型配置只检查变量是否存在；真实 API 健康检查留给应用日志或仓库既有健康检查机制。

### 6. 修改后的应用策略

说明普通 env 变更需要重建容器配置，推荐 `docker compose up -d --force-recreate agent-console`；涉及镜像构建参数时使用 `--build`；PostgreSQL 密码与已有数据卷不一致时不能仅修改 `.env`，需要按现有部署文档执行数据库密码同步。

### 7. 故障排查与扩展入口

用“现象 → 原因 → 修复”表覆盖规格中的常见错误。RAG、Qdrant、Telegram、飞书只给出链接或 profile 提示，不展开完整部署。

## 文档交互流程

```text
.env.example
      |
      v
复制/备份 .env → 填写最小配置 → 静态校验 → 重建容器
                         |             |
                         v             v
                 权限与变量检查   状态/日志/数据库检查
                         \             /
                          v           v
                           完成或按故障表修复
```

## 文件组织

```text
docs/
├── deployment/
│   ├── NEW_SERVER_ENV_GUIDE.md       # 新建：聚焦新服务器 .env 配置
│   └── SEOUL_SERVER_DEPLOYMENT.md     # 保持不变：完整部署参考
└── specs/2026-07-29-new-server-env-guide/
    ├── spec.md
    ├── plan.md
    ├── task.md                        # 下一阶段生成
    └── checklist.md                   # 验收设计阶段生成
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 文档位置 | 新建独立部署手册 | 便于新服务器操作者直接定位，避免完整部署文档过长 |
| 默认 Provider 示例 | OpenAI 兼容 `openai_relay` | 与当前 `.env.example` 和现有服务器部署文档一致 |
| 数据库拓扑 | Compose 内置 PostgreSQL | 与默认 `docker-compose.yml` 一致，不引入宿主机数据库分支 |
| Web 登录 | `WEB_USERS_JSON` 单管理员 | 支持明确角色并避免同时配置两套登录方式 |
| 上下文策略 | 新 TaskState 主路径全部开启 | 与当前代码默认和迁移 Runbook 一致 |
| Artifact 路径 | 容器内 `/app/.coding_applications/artifacts` | 与 `WORKDIR=/app` 的默认推导一致；后续需确保持久化策略清楚 |
| Compose 校验 | 使用 `docker compose config --quiet` | 能验证插值和语法，同时避免展开配置泄露秘密 |
| 真实配置变更 | 本任务只写文档 | 避免误改开发机或服务器的真实凭据与运行状态 |

## Spec 覆盖

- F1–F2：由“适用范围”“创建、备份与保护”覆盖。
- F3–F4：由“最小生产配置”“配置分组说明”覆盖。
- F5：由数据库分组和故障表覆盖。
- F6：由安全规则和无秘密验证流程覆盖。
- F7：由 TaskState 上下文配置分组覆盖。
- F8：由四层验证和应用策略覆盖。
- F9：由故障排查表覆盖。

未发现与 spec 冲突或未归属的功能需求。
