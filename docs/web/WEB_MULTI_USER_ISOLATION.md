# Web Multi-User Isolation

## 目标

本次改动把 taleclaw Web 控制台从“一个 Basic Auth 门禁后的共享工作区”升级为
“多个账号各自拥有私有数据边界”的 MVP。

改动前，只要通过同一组 `WEB_USERNAME` / `WEB_PASSWORD` 登录，就会共享：

- Web 会话
- `storage/` 文件区
- `memory/` 长期记忆
- 文本分析记录
- Bot 模式生成的报告和发布文件

改动后，Web 用户的数据进入独立目录，并且 Agent session 会携带当前用户身份：

```text
.users/<user-id>/
  storage/
  memory/
```

## 账号配置

### 单账号兼容模式

旧配置仍可使用：

```bash
WEB_USERNAME=agent
WEB_PASSWORD=replace-with-a-strong-password
```

这个账号会被视为 `admin`。数据目录变为：

```text
.users/agent/
```

### 多账号模式

多人部署时使用：

```bash
WEB_USERS_JSON={"admin":{"password":"change-admin","role":"admin"},"guest":{"password":"change-guest","role":"user"}}
```

支持两种角色：

| role | 能力 |
| --- | --- |
| `admin` | Web 聊天、私有文件、私有记忆、Coding 模式、服务器级定时任务 |
| `user` | Web 聊天、私有文件、私有记忆、Bot/Hybrid 受限工具 |

`WEB_USERS_JSON` 存在时优先使用它。不要同时保留两套配置，避免运维时误判实际生效的账号。

## 会话隔离

浏览器仍然只看到简单的会话 ID：

```text
default
web-lxyz123
```

后端保存时增加用户前缀：

```text
web:<user-id>:<chat-id>
```

例如：

```text
web:alice:default
web:bob:default
```

Web API 只会列出当前用户拥有的 `web:<user-id>:...` 会话。即使用户手工构造另一个
账号的完整 session ID，后端也会拒绝读取或删除。

## 文件和分析记录隔离

Web 文件 API 的逻辑路径保持不变：

```text
uploads/note.txt
generated/reports/daily.md
records/analysis.txt
```

实际路径按登录账号解析：

```text
.users/alice/storage/uploads/note.txt
.users/alice/storage/generated/reports/daily.md
.users/alice/storage/records/analysis.txt
```

文件列表、上传、预览、下载、新建目录、重命名和删除都使用相同边界。路径仍然经过
`resolve()` 和 `is_relative_to()` 检查，不能使用 `..` 或符号链接逃离用户目录。

## Agent 工具隔离

Web 请求进入 Agent runtime 时，会在 inbound metadata 中携带：

```json
{
  "user_id": "alice",
  "user_role": "user"
}
```

`AgentLoop` 将身份保存到 session metadata。以下 Bot 工具会根据 session 解析私有目录：

```text
storage_list_files
storage_read_file
storage_write_file
sandbox_list_files
sandbox_read_file
sandbox_write_file
publish_artifact
memorize
recall_memory
markdown_to_pdf
```

普通聊天沙盒原本已经根据 session ID 哈希隔离。现在 session ID 本身包含用户前缀，因此
不同用户即使都使用 `default` 会话，也会得到不同沙盒。

## 长期记忆隔离

新增 `ScopedMemoryStore`。普通 Web 会话构建上下文和执行记忆生命周期时，不再固定读取
仓库根目录的 `memory/`，而是根据当前 session 选择：

```text
.users/<user-id>/memory/
```

CLI 和没有用户 metadata 的内部旧链路仍然使用根目录 `memory/`，用于兼容现有本地工作流。

## 权限边界

Coding 模式包含 `bash`、项目文件读取和写入能力。对于多人 Web 控制台，它不是普通租户
能力。因此本阶段采用明确边界：

- `admin` 可以显式进入 Coding 模式，也可以由 Hybrid 路由进入 coding task。
- `user` 请求 `/coding` 时会收到权限提示，并保持 Bot 模式。
- `user` 的 Hybrid 模式不会自动进入 coding task。
- 服务器级 scheduler 工具标记为 `admin_only`，普通用户看不到也无法通过
  `tool_search` 解锁。

这是一套适合“管理员加少量受限用户”的 MVP 权限模型，不是面向不受信任公网用户的完整
容器租户系统。Coding 模式仍然操作同一份项目工作区，因此只应授予可信管理员。

## Markdown 转 PDF

`markdown_to_pdf` 现在是 session-scoped 插件工具：

- CLI 或没有 Web 用户 metadata 的旧链路仍使用仓库相对路径。
- Web 用户只能读取自己的 `storage/`，并只能把 PDF 写回自己的 `storage/`。

Web 用户可以使用：

```text
note.md
generated/note.pdf
storage/note.md
storage/generated/note.pdf
```

其中可选的 `storage/` 前缀会被解释为当前用户私有文件区，不是仓库根目录的共享
`storage/`。

## Docker 和迁移

`docker-compose.yml` 新增持久化目录：

```text
./.users:/app/.users
```

`.users/` 同时加入 `.gitignore` 和 `.dockerignore`，避免上传真实用户数据。

升级后，旧 `storage/` 和 `memory/` 不会自动分配给任意 Web 用户。迁移给管理员示例：

```bash
docker compose down
mkdir -p .users/admin/storage .users/admin/memory
cp -a storage/. .users/admin/storage/
cp -a memory/. .users/admin/memory/
docker compose up -d --build
```

## 涉及文件

核心新增：

```text
user_scope.py
memory/scoped_store.py
tests/test_multi_user_isolation.py
```

主要修改：

```text
web/server.py
web/static/index.html
web/static/app.js
core/runtime.py
core/agent_loop.py
core/context.py
memory/lifecycle.py
tools/handlers.py
tools/tool_registry.py
plugins/base.py
plugins/plugin_manager.py
plugins/markdown_pdf/plugin.py
plugins/scheduler/plugin.py
modes/router.py
tasksessions/session.py
tasksessions/runner.py
docker-compose.yml
.env.example
web/README.md
```

## 验证

执行：

```bash
python -m py_compile \
  user_scope.py \
  memory/scoped_store.py \
  web/server.py \
  core/bootstrap.py \
  core/context.py \
  core/runtime.py \
  core/agent_loop.py \
  modes/router.py \
  tools/tool_registry.py \
  tools/handlers.py \
  plugins/base.py \
  plugins/plugin_manager.py \
  plugins/markdown_pdf/plugin.py \
  plugins/status_commands/plugin.py \
  plugins/scheduler/plugin.py \
  tasksessions/session.py \
  tasksessions/runner.py \
  tests/test_multi_user_isolation.py

docker compose config --quiet
git diff --check
python -m unittest discover -s tests -v
```

结果：

```text
Ran 96 tests
OK
```

新增隔离测试覆盖：

- 多用户账号配置
- 两个用户的 Web 文件区互不可见
- 两个用户的分析记录互不可见
- Web 会话列表和单会话读取隔离
- Bot storage 和 memory 工具继承 session 用户
- 普通用户无法进入 Coding 模式
- 普通用户不可见管理员工具
- Markdown 转 PDF 只写入当前用户私有目录
