# Web 会话自动标题 Plan

## 架构概览

采用 Web 层 post-turn 标题服务。正文运行完成并保存后、SSE `complete` 发出前生成标题。该位置只影响 Web，会话正文的所有增量已经发出，同时标题能够进入 complete 结果。

系统划分为四部分：

- `WebSessionTitleService`：提取最早完整问答、调用 summary 路由、执行超时、规范化和本地兜底。
- `AgentWebService`：主回答结束后调用标题服务，再返回流式 complete；沿用现有每 Session 锁。
- Session 元数据/API 投影：继续使用现有 metadata JSON 保存标题，对外统一返回顶层 `title`。
- Web 前端：会话列表和当前标题显示 `title || chat_id`，身份、请求和删除操作仍使用 `chat_id`。

```text
正文模型增量 → SSE delta 持续发送
正文完成 → Session 首次保存
标题服务（最长等待 8 秒，失败则本地兜底）
标题写入 metadata → Session 再次保存
SSE complete 携带最新 title
```

## 核心数据结构

### Session 标题元数据

```python
session.metadata["title"]: str
```

只增加可选字段，不迁移数据库结构。缺失或空白表示尚未生成。

### `SessionTitleResult`

```python
@dataclass(frozen=True)
class SessionTitleResult:
    title: str
    source: Literal["existing", "model", "fallback", "unavailable"]
    updated: bool
```

该结果用于测试和记录生成路径，不写入消息历史。

### `WebSessionTitleService`

```python
class WebSessionTitleService:
    def __init__(
        self,
        *,
        runner: ModelTaskRunner,
        timeout_seconds: float = 8.0,
        max_chars: int = 20,
    ): ...

    async def ensure_title(self, session: Session) -> SessionTitleResult: ...
```

`ensure_title` 的行为顺序：

1. 已有标题时直接返回 `existing`。
2. 从消息中提取最早的 user 消息及其后第一条 assistant 消息。
3. 没有完整问答时返回 `unavailable`。
4. 在线程中使用现有 summary 路由执行一次无工具模型调用，并通过异步超时限制等待时间。
5. 规范化模型结果；失败、超时或空结果时规范化首个问题作为 fallback。
6. 把最终标题写入 `metadata.title`，返回生成来源。

### 纯函数

```python
def first_question_answer(messages: list[dict]) -> tuple[str, str] | None: ...
def normalize_session_title(text: str, *, max_chars: int = 20) -> str: ...
def session_display_title(session_or_row: Mapping, fallback_id: str) -> str: ...
```

规范化折叠空白、移除成对或首尾引号，并按 Python Unicode code point 截断到 20 字符，不追加省略号。

### 标题模型任务

使用 `AgentSpec(model_purpose="summary", max_tokens=48)`。Prompt 要求根据给定首个问题和回答输出单个主题标题，不超过 20 个字符，不使用外部上下文或工具。传给 runner 的 messages 是全新构造的数据，不包含 Session Context、长期记忆或后续消息。

## 模块设计

### `web/session_titles.py`

**职责：**

- 提取首组完整问答。
- 构造隔离的标题生成 Prompt。
- 调用 `ModelTaskRunner` 的 summary 路由。
- 处理 8 秒超时、异常、空结果和本地兜底。
- 规范化标题并写入 Session metadata。

**依赖：** `asyncio`、`AgentSpec`、`ModelTaskRunner` 和 Session 公共结构。不依赖 SessionStore、长期记忆、向量索引或前端。

### `web/server.py`

**职责：**

- Web Runtime 启动后，使用现有 `model_task_runner` 构造标题服务。
- `_ask_async` 在 `run_message()` 完成后调用 `ensure_title()`。
- 标题更新后通过现有 `SessionManager.save()` 持久化。
- `read_sessions()` 和 `read_session()` 将元数据标题投影为顶层 `title`。
- 流式 complete 继续读取已保存 Session，因此自然携带最新标题。

标题服务内部把模型异常转换为 fallback，不能让 `/api/chat/stream` 返回 error。没有完整问答或已有标题时不额外保存。

### `web/static/app.js`

**职责：**

- 会话列表显示 `session.title || session.chat_id`。
- 会话搜索同时匹配标题和 `chat_id`。
- 当前聊天页显示 `session.title || state.sessionId`。
- 收到 complete 后立即更新当前页标题，再刷新会话列表。
- 所有点击、加载、删除请求仍传递稳定的 `chat_id`。

标题通过 `textContent` 写入 DOM，维持现有转义边界。

## 模块交互

```text
AgentWebService._ask_async
  → runtime.run_message(... on_text)
      → 正文 delta 进入 SSE 队列
      → assistant 消息和 Session 保存
  → WebSessionTitleService.ensure_title(session)
      → first_question_answer
      → asyncio.to_thread(summary model call)
      → wait_for(timeout=8s)
      → normalize / fallback
  → SessionManager.save(session)
  → ask_stream 返回
  → complete 事件读取最新 Session
  → 前端更新标题
```

并发控制继续由 `AgentWebService._lock_for_session()` 提供。同一 Session 的第二次请求无法越过仍在执行的第一轮标题生成；后续进入服务时会看到现有标题并立即返回。

## 文件组织

```text
web/
├── session_titles.py              新增：标题生成、提取、规范化、兜底
├── server.py                      修改：post-turn 接入、API title 投影
└── static/app.js                  修改：列表与当前页显示 title

tests/
├── test_web_session_titles.py     新增：服务、超时、兜底、幂等、旧会话
└── test_web_streaming.py          修改：complete 携带标题、正文 delta 不受影响

.env.example                       修改：标题生成超时配置说明
```

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 标题存储 | `Session.metadata.title` | 无需 schema 迁移，兼容旧数据 |
| 接入层 | Web post-turn | 满足只处理 Web，不侵入其他入口 |
| 模型路由 | 现有 `summary` route | 复用当前供应商、fallback 和健康状态 |
| 调用方式 | `asyncio.to_thread` + `wait_for` | 同步模型客户端不会阻塞 Web 事件循环，可限制用户等待 |
| 超时后处理 | 立即保存本地 fallback | 正文成功优先，不因辅助能力失败 |
| 流式顺序 | 先发完正文 delta，再生成标题 | 保持首字延迟和正文流式体验 |
| 持久化 | 主回答保存后，仅在标题更新时再保存一次 | 不改变 AgentLoop，第二次保存不会重复插入未变化消息 |
| 字符限制 | 服务端强制 20 code points | 不依赖模型输出约束，API 与前端一致 |
| 身份与显示 | `chat_id` 负责身份，`title` 负责显示 | 避免重命名破坏路由、锁和历史 |
| 超时线程 | 生成函数保持纯计算，超时后结果丢弃 | 底层同步请求稍后结束时不能再修改 Session |

配置项：

```dotenv
WEB_SESSION_TITLE_TIMEOUT_SECONDS=8
```

标题最大长度固定为产品规则 20，不开放环境配置，避免不同实例产生不一致行为。

## Spec 覆盖

| 需求 | 设计归属 |
|---|---|
| F1、F2、F7 | `first_question_answer`、`WebSessionTitleService.ensure_title` |
| F3 | `normalize_session_title` |
| F4 | 服务超时、异常和 fallback 路径 |
| F5 | metadata 写入、existing 快速返回、Session 锁 |
| F6 | API title 投影、前端 `title || chat_id` |
| F8 | post-turn 保存、complete 读取最新 Session |
