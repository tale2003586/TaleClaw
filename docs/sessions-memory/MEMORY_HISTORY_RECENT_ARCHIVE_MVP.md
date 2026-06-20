# 主会话记忆分层与 SQLite 归档改动记录

## 一、改动目标

本次完成主会话记忆系统的三个 MVP 改进：

1. `memory/HISTORY.md` 完整保存用户输入，只保存 AI 回复摘要。
2. `memory/RECENT_CONTEXT.md` 从“只覆盖最新一轮”改为“保留最近多轮”的滚动窗口。
3. 从 Recent 窗口淘汰的旧轮次写入 SQLite `memory_archive` 表，为后续向量索引做准备。

原始会话 transcript 仍然保存在 `.sessions/sessions.db` 的 `sessions` 和 `messages`
表中。新的 History、Recent 和 Archive 都是从原始会话派生出的记忆层，不替代原始数据。

## 二、整体数据流

每轮主会话完成后，执行：

```text
Session 原始 user / assistant 消息
  -> 用户输入完整保留
  -> AI 回复生成一次摘要
  -> HISTORY.md 追加 USER + ASSISTANT_SUMMARY
  -> RECENT_CONTEXT.json 追加包含用户原文的结构化 turn
  -> RECENT_CONTEXT.md 重建有长度边界的可读视图
  -> 超出最近 6 轮的旧 turn 写入 SQLite memory_archive
```

AI 摘要只计算一次，History、Recent 和 Archive 复用同一个结果。

## 三、History 行为

### 修复前

`memory/HISTORY.md` 对用户输入和 AI 回复都进行最多 800 字符截断：

```text
USER: ...

ASSISTANT: ...
```

### 修复后

用户输入完整保存，AI 回复存摘要：

```text
USER:
用户完整输入

ASSISTANT_SUMMARY:
AI 回复摘要
```

短回复不调用模型，直接原样保存。长回复通过 `HistorySummarizer` 额外调用一次模型生成摘要。
当摘要调用失败时，会退化为本地截断，不阻塞正常对话。

当前参数：

```text
短回复直接保存阈值：240 字符
摘要最大保存长度：480 字符
摘要请求 max_tokens：220
```

## 四、Recent 滚动窗口

### 可读视图

`memory/RECENT_CONTEXT.md` 仍然保留，供上下文构造器和人工阅读使用：

```text
# Recent Context

## Turn 1

- session: `web:default`
- mode: `hybrid`
- source_ref: `web:default:7`
- created_at: `...`

### USER_EXCERPT

用户输入摘录

### ASSISTANT_SUMMARY

AI 回复摘要
```

### 结构化状态

新增运行时文件：

```text
memory/RECENT_CONTEXT.json
```

该文件保存 Recent 窗口的结构化 turn 列表，包含完整用户输入，避免依赖正则表达式解析 Markdown。
它是运行时派生状态，已经加入 `.gitignore`。

当前窗口大小：

```text
最近 6 轮
Markdown 中每轮用户摘录最多 1200 字符
```

上下文构造器读取的是有边界的 Markdown 视图，因此过长用户输入不会让工作上下文无限膨胀。
JSON 结构化状态和 SQLite 归档仍然保留完整用户输入。

升级前已有的 `RECENT_CONTEXT.md` 不会被反向解析。升级后的下一轮对话会开始建立新的
JSON 窗口，并重建 Markdown 视图。旧对话仍可在原始 Session SQLite 和 History 中查看。

## 五、SQLite Archive

新增：

```text
memory/archive_store.py
```

归档与 Session 共用：

```text
.sessions/sessions.db
```

新增表：

```sql
CREATE TABLE memory_archive (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id        TEXT NOT NULL,
    mode              TEXT NOT NULL,
    user_text         TEXT NOT NULL,
    assistant_summary TEXT NOT NULL,
    source_ref        TEXT NOT NULL UNIQUE,
    created_at        TEXT NOT NULL,
    archived_at       TEXT NOT NULL,
    metadata          TEXT NOT NULL DEFAULT '{}'
);
```

`source_ref` 使用 `session_id:assistant_message_index`，并带唯一约束。即使生命周期重复执行，
同一轮也不会重复归档。

可在服务器查看归档：

```bash
sqlite3 .sessions/sessions.db \
  "SELECT id, session_id, source_ref, created_at FROM memory_archive ORDER BY id DESC LIMIT 20;"
```

## 六、文件改动

新增：

```text
memory/archive_store.py
memory/history_summary.py
tests/test_memory_lifecycle_archive.py
docs/MEMORY_HISTORY_RECENT_ARCHIVE_MVP.md
```

修改：

```text
.gitignore
core/bootstrap.py
memory/lifecycle.py
memory/store.py
```

### `core/bootstrap.py`

主会话启动时创建 `MemoryArchiveStore`，并向 `MemoryLifecycle` 注入：

```text
HistorySummarizer(provider=provider, model=MODEL)
MemoryArchiveStore()
```

TaskSession 未注入全局 Archive，因此任务内的派生记忆仍然保持局部隔离。

### `memory/store.py`

新增 Recent JSON 的读写和 Markdown 渲染方法：

```text
read_recent_turns()
write_recent_turns()
```

旧方法 `write_recent_context()` 保留，供 TaskSession 初始化局部上下文使用。

### `memory/lifecycle.py`

新增：

```text
assistant reply 摘要
Recent 多轮窗口维护
溢出 turn SQLite 归档
archived_count 结果统计
```

## 七、测试覆盖

新增测试验证：

```text
History 保留完整用户输入
History 保存 AI 摘要
Recent 只保留配置数量的最新 turn
被淘汰的 turn 写入 SQLite
History、Recent、Archive 复用同一个摘要
短 AI 回复不触发额外 LLM 调用
Recent Markdown 有长度边界，但 JSON 保留完整用户输入
source_ref 去重防止重复归档
```

验证命令：

```bash
python3 -B -m unittest discover -s tests -v
git diff --check
python3 -B -m py_compile \
  memory/archive_store.py \
  memory/history_summary.py \
  memory/store.py \
  memory/lifecycle.py \
  core/bootstrap.py
```

## 八、后续向量数据库接入位置

本次只做 SQLite 冷归档，不直接引入向量数据库。后续可以基于 `memory_archive` 增加索引任务：

```text
memory_archive 新记录
  -> embedding worker
  -> vector store
  -> recall router 判断是否需要语义检索
  -> 将命中的少量历史片段注入上下文
```

建议将 SQLite 中的 `source_ref` 作为向量条目的稳定外部 ID，便于重建索引和追溯原始会话。
