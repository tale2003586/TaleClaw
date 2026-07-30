# 新服务器 `.env` 配置手册 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 新建 | `docs/deployment/NEW_SERVER_ENV_GUIDE.md` | 新服务器首次修改 `.env` 的独立操作手册 |
| 修改 | `docs/README.md` | 在部署文档索引中加入新手册入口 |
| 已有 | `docs/deployment/SEOUL_SERVER_DEPLOYMENT.md` | 完整部署流程的引用来源，不在本任务中改写 |
| 已有 | `.env.example` | 变量分组和示例来源，不在本任务中修改 |

## T1：建立手册骨架与安全前置步骤

**文件：** `docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** 无

**步骤：**

1. 写明 Ubuntu、Docker Compose、`/opt/taleclaw` 的适用范围及路径替换规则。
2. 链接完整服务器部署手册，明确本文不重复 Docker、Nginx 和 HTTPS 安装。
3. 添加进入项目目录、检查 `.env.example`、首次复制和已有 `.env` 备份步骤。
4. 设置 `.env` 权限为 `600`，加入禁止提交、打印和分享真实配置的提示。

**验证：** 检查命令块均使用明确项目路径，首次创建与已有文件升级两种情况均有步骤，且没有真实凭据。

## T2：编写最小生产配置模板

**文件：** `docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** T1

**步骤：**

1. 添加 OpenAI 兼容 Provider、context window 和单 Provider 路由示例。
2. 添加 PostgreSQL Compose 配置与共用数据库 URL 示例。
3. 添加 `WEB_USERS_JSON` 管理员、关闭注册/匿名访问和 Cookie 开关。
4. 添加 `/app` 容器工作区配置。
5. 添加当前 TaskState、语义压缩、Artifact 外置、动态预算、Prompt 比例和长内容阈值。
6. 默认关闭 RAG、历史向量和 Security RAG 可选功能。
7. 所有秘密值使用明显的 `REPLACE_*` 占位符。

**验证：** 逐项对比 `.env.example`、`docker-compose.yml` 和 `config.py`；确认变量拼写正确、非秘密默认值一致、占位符不会被误认为有效生产密码。

## T3：解释配置分类和容器路径

**文件：** `docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** T2

**步骤：**

1. 将配置分为“必须修改”“建议保持默认”“按需启用”。
2. 解释 Provider base URL、wire API、模型名和上下文窗口的配套关系。
3. 解释 `postgres:5432` 与 `127.0.0.1:55432` 的使用边界和 DSN 特殊字符编码。
4. 解释宿主机 `/opt/taleclaw` 与容器 `/app` 的区别。
5. 解释新 TaskState 主路径和动态预算，明确不再调整已忽略的旧固定压缩阈值。
6. 说明 Artifact 目录需要持久化，并核对当前 Compose 是否已经为该目录提供持久化挂载；若没有，明确记录风险与处理方式。

**验证：** 每组关键变量都有分类和用途说明；数据库、工作区、Artifact 三类路径不会混用。

## T4：编写无秘密泄露的验证与应用步骤

**文件：** `docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** T3

**步骤：**

1. 添加文件权限、Git 忽略状态和必要变量是否为空的检查。
2. 使用 `docker compose config --quiet` 检查 Compose 解析，不输出展开后的配置。
3. 添加首次启动和 env 修改后的 `--force-recreate` 操作；区分需要 `--build` 的镜像构建变量。
4. 添加 `docker compose ps`、有限行数日志和 PostgreSQL `pg_isready` 检查。
5. 在容器内只输出非敏感开关或“已设置/未设置”状态，不打印 API Key、登录密码和完整 DSN。
6. 添加 HTTPS 启用后切换 `WEB_COOKIE_SECURE=1` 的提示。

**验证：** 搜索验证命令，确认不存在会直接展开整个 `.env`、API Key、密码或完整数据库 URL 的命令。

## T5：编写故障排查和可选功能入口

**文件：** `docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** T4

**步骤：**

1. 用“现象 / 原因 / 修复”表覆盖数据库容器地址错误。
2. 覆盖已有 PostgreSQL 数据卷与新密码不一致。
3. 覆盖 Provider base URL、wire API、model 不匹配。
4. 覆盖宿主机与容器路径混淆。
5. 覆盖修改 `.env` 后未重建容器。
6. 增加 RAG/Qdrant、Telegram、飞书的完整部署文档入口，不展开其配置细节。

**验证：** 故障表至少包含五类问题，每项都有可执行且非破坏性的首选修复方向。

## T6：加入文档索引并执行一致性检查

**文件：** `docs/README.md`、`docs/deployment/NEW_SERVER_ENV_GUIDE.md`
**依赖：** T5

**步骤：**

1. 在 `docs/README.md` 的部署相关区域加入新手册链接。
2. 检查手册中的所有相对链接均指向存在的文件。
3. 从配置模板提取变量名，与仓库配置来源交叉搜索。
4. 扫描占位符、真实路径、疑似秘密和过时 Coding 固定压缩参数。
5. 运行 Markdown 基础格式检查或等价的人工结构检查。
6. 查看 Git diff，确认仅包含规格文档、新手册和索引修改。

**验证：** 链接目标存在；模板变量均有代码、Compose 或示例来源；文档不包含旧固定压缩参数或真实秘密；Git diff 范围符合 spec。

## 执行顺序

```text
T1 → T2 → T3 → T4 → T5 → T6
```

所有任务按文档阅读顺序串行执行，避免后续说明与前面的配置模板脱节。
