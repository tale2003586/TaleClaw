# Phase 16：Session SQLite 持久化说明

这次 Phase 16 的目标是把原来的内存 SessionManager 改成 SQLite 持久化。

改进前：

```text
SessionManager
  只存在内存里
  退出 CLI 后对话历史丢失
```

改进后：

```text
SessionManager
  内存缓存当前 session
  同时把 session 和 messages 保存到 .sessions/sessions.db
  重启后可以通过 session_id 重新加载历史
```

---

## 一、这次新增了什么

### 1. 新增 `session_store.py`

新增文件：

```text
session_store.py
```

它负责 SQLite 读写，不负责 agent 逻辑。

里面有：

```python
class SessionStore:
    def load_session(session_id)
    def save_session(session)
    def list_sessions()
    def close()
```

数据库默认位置：

```text
.sessions/sessions.db
```

---

### 2. 新增 sessions 表

用于保存 session 元数据：

```sql
sessions (
    id,
    current_mode,
    created_at,
    updated_at,
    last_compacted,
    metadata
)
```

这些字段对应你当前的 `Session`：

```python
current_mode
created_at
updated_at
last_compacted
metadata
```

也就是说，模式切换、创建时间、更新时间、压缩时间都会被保存。

---

### 3. 新增 messages 表

用于保存对话消息：

```sql
messages (
    session_id,
    seq,
    role,
    timestamp,
    message_json
)
```

其中 `message_json` 保存完整 message dict。

这么做是为了保留：

```text
普通 user/assistant 消息
assistant tool_calls 原始结构
tool result
hook trace
metadata
```

对于 agent 来说，这很重要。因为 tool loop 需要完整的 assistant `tool_calls` 和后续 `tool` 消息能对应上。

---

## 二、改了哪些文件

### `session.py`

`SessionManager` 从纯内存变成：

```text
内存缓存 + SQLite store
```

现在：

```python
SessionManager()
```

会自动使用：

```text
.sessions/sessions.db
```

`get_or_create(session_id)` 会先查内存，没有再查 SQLite，SQLite 也没有才新建。

`save(session)` 会保存到 SQLite。

---

### `session_store.py`

这是新抽出来的持久化层。

它的职责是：

```text
建表
加载 session
保存 session
列出 session
关闭数据库连接
```

它不应该知道：

```text
LLM
Pipeline
Tool
ModeRouter
MessageBus
```

这样职责比较干净。

---

## 三、当前实现策略

第一版采用的是“保存整个 session 快照”。

也就是说，每次：

```python
sessions.save(session)
```

会：

```text
1. upsert sessions 表
2. 删除该 session 旧 messages
3. 按当前 session.messages 重新插入 messages
```

这不是最高性能方案，但第一版非常稳：

```text
逻辑简单
不容易出现 seq 错乱
适合当前小型 agent harness
```

后续如果消息很多，可以再改成 append-only。

---

## 四、你现在需要掌握什么

你的目标是找“大模型应用开发”实习，所以这里的学习重点不是 SQLite 语法本身，而是下面这些工程概念。

### 1. 会话状态和模型上下文不是一回事

Session 保存的是：

```text
长期对话历史
模式
metadata
tool traces
```

ContextBuilder 构建的是：

```text
本轮发给模型看的 messages
```

这两者要分开。

面试/实习里常见问题：

> 怎么让 LLM 应用支持多轮对话？

你应该能回答：

```text
用 session_id 区分会话，把历史消息持久化保存；
每轮请求时加载对应 session，再经过上下文构建和裁剪后发给模型。
```

---

### 2. 为什么 tool_calls 要完整保存

LLM tool calling 的消息格式有约束：

```text
assistant message with tool_calls
  后面必须接对应 tool result
```

所以不能只保存 assistant 的 content。

必须保留完整 raw message：

```text
tool_calls
tool_call_id
function name
arguments
tool result
```

否则下一轮发给模型时，可能出现：

```text
Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

这类错误。

---

### 3. 为什么要把持久化层抽出来

现在是：

```text
SessionManager
  负责缓存和业务接口

SessionStore
  负责 SQLite 读写
```

这就是常见的工程分层。

大模型应用开发不是只会调 API，还要能把：

```text
runtime
session
memory
tool
provider
storage
```

拆成清晰模块。

---

### 4. SQLite 在 LLM 应用里的典型用途

SQLite 很适合做本地 agent 的第一版持久化：

```text
sessions.db        保存会话
memory.db          保存长期记忆
observe.db         保存 trace
tasks.db           保存任务
proactive.db       保存主动推送状态
```

你不需要一开始就上 Postgres、Redis、向量库。

第一版产品/原型里，SQLite 很实用。

---

### 5. 你要能讲清楚这一条链路

现在你的系统可以这样讲：

```text
InboundMessage
  -> AgentLoop 根据 session_key 找 Session
  -> SessionManager 从 SQLite 加载或新建 Session
  -> Pipeline 运行模型和工具
  -> SessionManager.save 写回 SQLite
  -> OutboundMessage 发回用户
```

这就是一个小型 LLM runtime 的核心闭环。

---

## 五、后续可以怎么继续改

### 1. 新增 `/sessions`

列出历史 session：

```python
sessions.list_sessions()
```

可以让 CLI 支持：

```text
/sessions
```

查看有哪些会话。

### 2. 新增 `/new`

开始一个新会话：

```text
cli:local:timestamp
```

这样不是所有 CLI 对话都挤在 `cli:local`。

### 3. 改成 append-only

当前保存策略是快照式。

未来可以优化为：

```text
只追加新增 messages
不每次 delete + insert
```

但现在不急。

### 4. 对 messages 做窗口恢复

后续可以学习 akashic-agent 的做法：

```text
按 user boundary 恢复历史
避免从 role=tool 的消息开头截断
工具结果做长度截断
```

这会让上下文更稳定。

---

## 六、面向实习的学习重点

如果你要投“大模型应用开发”实习，这个 Phase 16 背后的重点是：

```text
1. 会话管理
2. 多轮对话持久化
3. tool calling 消息格式
4. 状态恢复
5. SQLite 本地存储
6. runtime 分层设计
```

你不只是“会调模型 API”，而是在搭一个 agent runtime。

你应该能在简历或面试中描述：

> 我实现了一个消息驱动的 LLM agent runtime，支持 session 持久化、模式路由、工具注册、工具调用 hook、上下文构建和 SQLite 会话恢复。

这比只写“调用 OpenAI API 做聊天机器人”强很多。

