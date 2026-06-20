# Bot 模式受限文件区第一阶段改动记录

## 一、目标

Bot 模式需要支持日常文档生成，例如：

```text
请整理一份 AI 日报并保存为 Markdown。
```

此前只有 Coding 模式能够调用通用的 `write_file` 和 `edit_file`。这两个工具可以修改
整个项目工作区，因此不适合直接开放给 Bot 模式。

本阶段新增一组受限工具，让 Bot 可以读取私有文件区，并且只能在：

```text
storage/generated/
```

下新建文本产物。

## 二、新增工具

### `storage_list_files`

列出 `storage/` 中的文件和目录。

```json
{
  "path": "uploads"
}
```

`path` 可省略。工具输入和返回路径均相对于 `storage/`。

### `storage_read_file`

读取 `storage/` 中的 UTF-8 文本文件。

```json
{
  "path": "uploads/source.txt",
  "limit": 100
}
```

### `storage_write_file`

在 `storage/generated/` 中新建 UTF-8 文本文件。

```json
{
  "path": "reports/ai-daily-2026-05-31.md",
  "content": "# AI 日报\n\n..."
}
```

这里的 `path` 相对于 `storage/generated/`。成功后返回：

```json
{
  "status": "created",
  "path": "generated/reports/ai-daily-2026-05-31.md",
  "bytes": 18
}
```

## 三、安全边界

Bot 模式仍然无法调用：

```text
bash
write_file
edit_file
```

新增工具具有以下限制：

- `storage_list_files` 和 `storage_read_file` 只能访问 `storage/` 内部。
- `storage_write_file` 只能写入 `storage/generated/`。
- 拒绝绝对路径、`../` 路径穿越和隐藏路径。
- 默认且始终拒绝覆盖已有文件，避免静默替换用户内容。
- 单次读取最大 `1,000,000` 字节。
- 单次写入最大 `10 MiB`。
- 写入格式限制为文本类文件：`.md`、`.txt`、`.json`、`.csv`、`.html`、
  `.yaml`、`.yml`、`.xml` 和 `.log`。
- 文件先写入同目录临时文件，再替换为正式文件，减少半成品。

复杂任务需要的 `.task_sandbox/<task_id>/` 临时目录和 `publish_artifact` 工具不在
本阶段实现范围内。

## 四、写入记录

每次成功调用 `storage_write_file` 后，都会追加一条 JSONL 记录：

```text
storage/records/storage_writes.jsonl
```

记录内容包括：

```json
{
  "timestamp": "2026-05-31T00:00:00+00:00",
  "session_id": "web:default",
  "path": "generated/reports/ai-daily-2026-05-31.md",
  "bytes": 18,
  "sha256": "..."
}
```

记录不保存正文，避免复制大量内容或扩大敏感信息暴露面。

## 五、模式可见性

三个工具在 Bot 模式中默认可见，无需先调用 `tool_search`。

Coding 模式也允许使用这些工具，但默认仍保留原有的通用工作区工具，不额外增加
首轮上下文负担。需要时可以通过：

```text
tool_search(query="select:storage_write_file")
```

解锁对应工具。

## 六、文件改动

新增：

```text
tests/test_bot_storage_tools.py
docs/BOT_STORAGE_ARTIFACT_TOOLS.md
```

修改：

```text
tools/schema.py
tools/handlers.py
tools/tool_registry.py
web/README.md
```

## 七、测试

运行：

```bash
python -m unittest discover -s tests -v
```

新增测试覆盖：

- Bot 模式可见受限 storage 工具，但不可见 `bash`、`write_file` 和 `edit_file`
- 写入报告并生成 JSONL 审计记录
- 拒绝 `../` 路径穿越
- 拒绝覆盖已有文件
- 拒绝超出大小限制的文件
- 拒绝非文本文件后缀
- 读取和列出 `storage/` 中的文本文件

## 八、后续阶段

第二阶段建议增加：

```text
.task_sandbox/<task_id>/
publish_artifact
storage_delete_file
```

复杂任务先在临时沙盒中生成中间文件，再显式发布最终产物。删除文件仍应要求用户确认。
