# Qdrant 历史会话向量库接入说明

这份文档说明当前项目中 Qdrant 的正确职责边界：

- `MEMORY.md`、`HISTORY.md`、`PENDING.json` 仍然只存储在本地文件中。
- Qdrant 不作为稳定记忆库，不存“用户偏好”这种抽象后的 memory item。
- Qdrant 只存完整历史会话 turn，包括 user、assistant、assistant.tool_calls、tool result，以及当轮 metadata。

换句话说：本地 `.md/.json` 是“人类可读记忆系统”，Qdrant 是“完整会话历史的语义索引和召回系统”。

## 为什么这样分层

本地文件适合存储可审计、可编辑的长期记忆：

- `MEMORY.md`：稳定记忆。
- `PENDING.json`：候选记忆。
- `HISTORY.md`：可读的对话摘要。
- `RECENT_CONTEXT.json`：近期窗口数据。

Qdrant 适合存储完整历史 turn：

- 可以保留完整 tool call / tool result。
- 可以用当前问题召回相关旧 turn。
- 可以作为 trace/debug 的历史索引。
- 后续多模态时，可以把图片、OCR、文件片段作为同一 turn 的 payload 或 named vector 扩展。

## 启动 Qdrant

项目已有 `docker-compose.yml` 中的 `qdrant` 服务：

```bash
docker compose up -d qdrant
```

本地访问：

```text
HTTP API: http://127.0.0.1:6333
Web UI:   http://127.0.0.1:6333/dashboard
gRPC:     127.0.0.1:6334
```

## 环境变量

默认关闭。启用时在 `.env` 中加入：

```env
HISTORY_VECTOR_ENABLED=1
HISTORY_VECTOR_BACKEND=qdrant
QDRANT_URL=http://127.0.0.1:6333
QDRANT_API_KEY=
QDRANT_COLLECTION=taleclaw_history
QDRANT_VECTOR_SIZE=512
QDRANT_DISTANCE=Cosine

EMBEDDING_PROVIDER=hash
EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5

HISTORY_RETRIEVAL_TOP_K=6
HISTORY_RETRIEVAL_MIN_SCORE=0.35
CONTEXT_RETRIEVED_HISTORY_BUDGET=3000
CONTEXT_RETRIEVED_HISTORY_FLOOR=500
```

建议：

1. 先用 `EMBEDDING_PROVIDER=hash` 做流程烟测。它不下载模型，适合验证写入、召回、trace。
2. 流程跑通后改成 `EMBEDDING_PROVIDER=fastembed`。`BAAI/bge-small-zh-v1.5` 是中文模型，FastEmbed 会自动使用模型真实维度。

## 代码结构

新增/修改的核心文件：

- `memory/embeddings.py`：embedding provider。
- `memory/vector_index.py`：向量历史记录接口，包含 `MemoryRecord` / `MemoryHit`。
- `memory/qdrant_index.py`：Qdrant collection 创建、upsert、query。
- `memory/vector_runtime.py`：从环境变量构建历史向量索引，以及 session scope 映射。
- `memory/lifecycle.py`：每轮对话结束后，把完整 turn 写入 Qdrant。
- `runtime/context.py`：构造上下文时召回历史 turn，注入 `<retrieved_history>`。
- `runtime/context_budget.py`：为 `retrieved_history` 增加上下文预算。
- `runtime/bootstrap.py`：启动时接入 history vector index。

## 写入 Qdrant 的数据

每轮对话结束时，系统会找到最后一个 user message，并从它开始截取到当前 session 末尾：

```text
user current request
assistant tool_call
tool result
assistant final answer
```

然后写入一条 `source_type=session_turn` 的向量记录。

结构大致是：

```python
MemoryRecord(
    id="session_turn:web:test:7:xxxx",
    text="user: ...\nassistant.tool_call: ...\ntool_result[call_1]: ...\nassistant: ...",
    scope="user:alice",
    source_type="session_turn",
    source_ref="web:test:7",
    metadata={
        "session_id": "web:test",
        "mode": "hybrid",
        "message_count": 4,
        "messages": [
            {"role": "user", "content": "..."},
            {"role": "assistant", "content": None, "tool_calls": [...]},
            {"role": "tool", "tool_call_id": "call_1", "content": "..."},
            {"role": "assistant", "content": "..."}
        ],
        "assistant_summary": "...",
        "created_at": "..."
    },
)
```

注意两点：

- `text` 用于 embedding 和语义搜索。
- `metadata.messages` 保留完整原始结构，包含完整工具调用和工具结果。

## 不写入 Qdrant 的数据

以下内容仍然只走本地文件系统：

- 显式记忆：`MEMORY.md`
- 候选记忆：`PENDING.json`
- 晋升后的候选记忆：`MEMORY.md`
- 可读历史摘要：`HISTORY.md`

这些内容不会单独作为 `stable_memory`、`candidate_memory`、`turn_summary` 写入 Qdrant。

## 候选记忆处理装置

候选记忆现在不再靠关键词直接判断。系统里有一个 `MemoryProcessingDevice`，它的职责是：

1. 扫描本轮用户描述。
2. 用这段描述去 Qdrant 的历史会话库中检索相似 `session_turn`。
3. 如果相似历史 turn 数量达到阈值，才把本轮用户描述写入 `PENDING.json`。
4. 如果候选记忆多次被相似描述触发，达到晋升条件后，交给 LLM 提炼稳定记忆。
5. LLM 输出的稳定记忆写入本地 `MEMORY.md`。

当前代码把这个装置做成独立组件，并在 `MemoryLifecycle.after_turn(...)` 中调用。也就是说，它已经和主记忆文件读写逻辑解耦；后续如果要改成真正的后台线程/任务队列，可以直接把 `MemoryProcessingDevice.process_user_description(...)` 调度到 worker 中，而不用改候选记忆和晋升规则本身。

相关配置：

```env
MEMORY_CANDIDATE_SIMILAR_TOP_K=8
MEMORY_CANDIDATE_SIMILAR_MIN_SCORE=0.55
MEMORY_CANDIDATE_SIMILAR_MIN_HITS=2
MEMORY_CANDIDATE_PROMOTION_CONFIDENCE=0.85
MEMORY_CANDIDATE_PROMOTION_EVIDENCE_COUNT=3
MEMORY_CANDIDATE_EXTRACT_MAX_TOKENS=220
```

这条链路的重点是：

- Qdrant 仍然只存完整历史 turn。
- `PENDING.json` 仍然是本地候选记忆文件。
- `MEMORY.md` 仍然是本地稳定记忆文件。
- Qdrant 只参与“这个用户描述是否在历史中反复出现过”的证据检索。
- 晋升写入 `MEMORY.md` 的内容不是候选原文，而是 LLM 从候选和相似历史证据中提炼后的稳定表述。

候选记录中的 metadata 会带上选择证据：

```json
{
  "selection": {
    "method": "history_vector_similarity",
    "similar_min_hits": 2,
    "similar_min_score": 0.55,
    "similar_hit_count": 3
  },
  "similar_history": [
    {
      "id": "session_turn:web:test:7:xxxx",
      "score": 0.82,
      "source_type": "session_turn",
      "source_ref": "web:test:7",
      "message_count": 4
    }
  ]
}
```

## 召回流程

每次模型调用前，`ContextBuilder.build(...)` 会：

1. 切分历史消息和 active turn。
2. 提取当前请求 `current_request`。
3. 用当前请求搜索 Qdrant 中的 `session_turn`。
4. 按 `scope` 过滤，避免串用户或串任务。
5. 把命中的旧 turn 渲染为 `<retrieved_history>`。
6. 将 `<retrieved_history>` 放在 active turn 之前。

prompt 顺序大致是：

```text
system
conversation_history
context_frame:
  <memory>本地 md 记忆</memory>
  <retrieved_history>Qdrant 召回的完整历史 turn 摘要文本</retrieved_history>
  <inbox>...</inbox>
active_turn:
  user current request
  assistant tool_call
  tool result
```

这个顺序是为了避免召回结果被误判为最新用户输入，也避免破坏 tool call / tool result 的顺序。

## Trace 中怎么看

上下文构造 trace 里会出现：

```json
{
  "retrieved_history": {
    "raw_chars": 1200,
    "rendered_chars": 900,
    "budget_chars": 3000,
    "truncated": false,
    "metadata": {
      "transport": "context_frame",
      "hit_count": 1,
      "hits": [
        {
          "id": "session_turn:web:test:7:xxxx",
          "score": 0.91,
          "source_type": "session_turn",
          "source_ref": "web:test:7",
          "message_count": 4
        }
      ],
      "budget_enabled": true,
      "strategy": "head_tail",
      "floor_chars": 500
    }
  }
}
```

这里能确认：

- 有没有召回历史 turn。
- 召回的是哪一轮。
- 相似度分数是多少。
- 该 turn 包含多少条完整消息。

记忆处理生命周期还会额外记录这些事件：

```text
memory.candidate.evaluated
memory.candidate.upserted
memory.candidate.promoted
memory.history_vector.upserted
memory.history_vector.failed
memory.lifecycle.completed
```

事件含义：

- `memory.candidate.evaluated`：本轮用户描述是否通过历史向量相似度检查。payload 会包含 `similar_hit_count`、`similar_hits`、`candidate_selected`、阈值配置等。
- `memory.candidate.upserted`：用户描述被写入或更新到 `PENDING.json`。
- `memory.candidate.promoted`：候选满足晋升条件，LLM 提炼后写入 `MEMORY.md`。
- `memory.history_vector.upserted`：完整 session turn 已写入 Qdrant。
- `memory.history_vector.failed`：写 Qdrant 失败，payload 包含错误类型和错误消息。
- `memory.lifecycle.completed`：本轮记忆生命周期的汇总计数。

`trace_summary.json` / `trace_summary.md` 里也会有 `memory` 汇总：

```json
{
  "memory": {
    "candidate_evaluations": 1,
    "candidate_upserts": 1,
    "candidate_promotions": 1,
    "history_vector_upserts": 1,
    "history_vector_failures": 0,
    "lifecycle_completed": 1,
    "last_similar_hit_count": 2,
    "last_candidate_selected": true,
    "promoted_previews": ["这个项目写测试时优先使用 pytest。"]
  }
}
```

## Scope 隔离

当前规则在 `memory/vector_runtime.py`：

```python
if session.metadata["kind"] == "task_session":
    scope = f"task:{task_id}"
else:
    scope = f"user:{user_id}"
```

普通对话按用户隔离；task session 按任务隔离。后续如果要做“主 agent 可读项目级历史，子 agent 只写任务级历史”，可以扩展成多 scope 检索：

```text
read_scopes = ["user:alice", "project:mytry", "task:xxx"]
write_scope = "task:xxx"
```

## 多模态扩展

后续可以把同一个历史 turn 扩展为多向量：

- `text`：文本消息和 tool result 的 embedding。
- `image`：图片或截图 embedding。
- `ocr`：图片 OCR 文本 embedding。

payload 中可以保留：

```json
{
  "messages": [...],
  "attachments": [
    {
      "type": "image",
      "path": "storage/screenshots/abc.png",
      "ocr_text": "...",
      "caption": "..."
    }
  ]
}
```

这样 Qdrant 仍然是“完整历史会话记录索引”，只是每条历史记录可以有文本、图片、OCR 等多个检索入口。

## 验证方法

安装依赖：

```bash
pip install -r requirements.txt
```

启动 Qdrant：

```bash
docker compose up -d qdrant
```

开启 hash 烟测：

```env
HISTORY_VECTOR_ENABLED=1
EMBEDDING_PROVIDER=hash
```

运行几轮带工具调用的对话后，检查：

- Qdrant collection 是 `taleclaw_history`。
- point 的 `payload.source_type` 是 `session_turn`。
- point 的 `payload.metadata.messages` 包含完整 user / assistant / tool 消息。
- trace 里有 `context_report.sections.retrieved_history`。

## 当前限制

- `hash` embedding 只能用于流程验证，不适合真实语义召回。
- `fastembed` 第一次会下载模型，国内网络可能需要镜像或代理。
- 当前每个 turn 写一条记录；如果 tool result 特别大，后续可以把单个 turn 进一步切 chunk，同时保留同一个 `turn_id`。
- 当前检索只查一个 scope；项目级、用户级、任务级多 scope 合并检索后续再扩展。

## 参考

- Qdrant Quickstart: https://qdrant.tech/documentation/quickstart/
- Qdrant Collections: https://qdrant.tech/documentation/manage-data/collections/
- Qdrant Vectors: https://qdrant.tech/documentation/manage-data/vectors/
- FastEmbed Supported Models: https://qdrant.github.io/fastembed/examples/Supported_Models/
