# Bot 临时沙盒与显式发布改动记录

## 一、目标

第一阶段允许 Bot 直接将简单报告写入：

```text
storage/generated/
```

对于需要反复整理、修改草稿或生成多个中间文件的任务，直接把所有内容写进用户文件区会
产生噪音。本阶段增加一个独立的临时沙盒，并要求 Bot 显式发布最终文件。

```text
.task_sandbox/       临时工作区
storage/generated/   用户可见的最终产物
```

## 二、目录结构

普通 Web 或 CLI 会话使用稳定的会话哈希隔离目录：

```text
.task_sandbox/sessions/<session-sha256-prefix>/
```

内部 Coding TaskSession 和 Scheduled Agent 使用可读的任务 ID：

```text
.task_sandbox/tasks/<task_id>/
```

普通会话目录不直接使用原始 `session_id`，避免将外部输入拼入文件路径。不同会话无法通过
工具访问彼此的临时文件。

## 三、新增工具

### `sandbox_list_files`

列出当前会话沙盒中的文件：

```json
{
  "path": "drafts"
}
```

### `sandbox_read_file`

读取当前会话沙盒中的 UTF-8 文本文件：

```json
{
  "path": "drafts/report.md",
  "limit": 200
}
```

### `sandbox_write_file`

写入草稿或中间文件：

```json
{
  "path": "drafts/report.md",
  "content": "# 草稿\n\n..."
}
```

沙盒文件默认不会被覆盖。修改已有草稿时必须显式设置：

```json
{
  "path": "drafts/report.md",
  "content": "# 修订版\n\n...",
  "overwrite": true
}
```

### `publish_artifact`

将选定的最终文件复制到 `storage/generated/`：

```json
{
  "source_path": "drafts/report.md",
  "destination_path": "reports/weekly-ai-report.md"
}
```

`destination_path` 可省略，默认沿用 `source_path`。发布操作始终拒绝覆盖已有文件。

## 四、典型流程

```text
用户：整理我上传的资料，先形成草稿，再发布一份最终报告。

Bot:
1. storage_read_file("uploads/source.md")
2. sandbox_write_file("drafts/report.md", "...")
3. sandbox_write_file("drafts/report.md", "...", overwrite=true)
4. publish_artifact("drafts/report.md", "reports/final-report.md")

最终文件：
storage/generated/reports/final-report.md
```

简单任务仍然可以直接调用 `storage_write_file`，无需强制经过沙盒。

## 五、安全边界

- Bot 模式仍然不能调用 `bash`、通用 `write_file` 和通用 `edit_file`。
- 沙盒工具必须绑定到活动会话，不能脱离 Session 单独调用。
- 沙盒路径拒绝绝对路径、`../` 路径穿越和隐藏路径。
- 沙盒文件默认拒绝覆盖，修订草稿时需要 `overwrite=true`。
- 沙盒文本单次写入最大 `10 MiB`。
- 发布文件最大 `50 MiB`。
- `publish_artifact` 只能发布普通文件，不能发布目录。
- 最终文件只能写入 `storage/generated/`，且不能覆盖已有文件。
- 如果 `storage/generated/` 被替换为指向 `storage/` 外部的符号链接，发布和直接写入都会拒绝。

## 六、自动清理

运行时启动以及每次使用沙盒工具时，都会清理过期的会话沙盒。默认保留：

```text
168 小时
```

可以在 `.env` 中调整：

```bash
TASK_SANDBOX_TTL_HOURS=168
```

设置为 `0` 时关闭 TTL 清理。每次读取、写入、列出或发布文件都会刷新当前沙盒的活动时间。

## 七、发布记录

直接生成文件和从沙盒发布文件都会记录在：

```text
storage/records/storage_writes.jsonl
```

发布记录示例：

```json
{
  "timestamp": "2026-05-31T00:00:00+00:00",
  "operation": "publish_artifact",
  "session_id": "web:default",
  "path": "generated/reports/final-report.md",
  "bytes": 1024,
  "sha256": "...",
  "source_path": "drafts/report.md"
}
```

记录不保存正文。

## 八、前端删除确认

前端文件区已有删除确认：

```javascript
window.confirm(`删除 ${entry.name}？`)
```

因此本阶段没有向 Bot 暴露 `storage_delete_file`。用户仍然可以在文件区手工删除文件，
而 Bot 不能自行删除最终产物。

## 九、Docker 持久化

`docker-compose.yml` 新增：

```text
./.task_sandbox:/app/.task_sandbox
```

这样容器更新时不会立即丢失仍在处理中的草稿。过期内容由 TTL 清理策略回收。

## 十、文件改动

新增：

```text
tests/test_bot_sandbox_tools.py
docs/BOT_TASK_SANDBOX_AND_PUBLISH.md
```

修改：

```text
.dockerignore
.env.example
.gitignore
core/bootstrap.py
docker-compose.yml
tools/handlers.py
tools/schema.py
tools/tool_registry.py
web/README.md
```

## 十一、测试

运行：

```bash
python -m unittest discover -s tests -v
```

新增测试覆盖：

- Bot 模式默认可见沙盒和发布工具
- 普通会话使用相互隔离的沙盒目录
- 内部 TaskSession 使用 `task_id` 目录
- 草稿覆盖必须显式设置 `overwrite=true`
- 路径穿越拒绝
- 发布最终文件并记录审计信息
- 重复发布拒绝覆盖
- `storage/generated/` 符号链接逃逸拒绝
- TTL 只清理过期沙盒
