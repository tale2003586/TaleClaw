# Task Memory 混合提取与日志隔离改动记录

## 一、改动目标

Coding TaskSession 需要同时满足两个目标：

1. 保留足够详细的执行日志，方便排查、复盘和审计。
2. 只把稳定、简短、可复用的结论提升到全局 `memory/PENDING.md`。

修复前，任务结束时会无条件把 `RECENT_CONTEXT.md` 和最终回复提升到全局
`PENDING.md`。这会将系统包装文本、全局记忆快照、寒暄、完整回复和临时状态混入
长期记忆候选。

本次改为混合模式：

```text
完整任务过程
  -> .task_sessions/<task-id>/TASK_LOG.md

Agent 主动记录的局部 pending
  + LLM 在任务结束时提取的结构化 conclusions
  -> 规则过滤
  -> 文本去重
  -> memory/PENDING.md
```

全局 `MEMORY.md` 不会被自动修改。候选项仍然需要后续人工审核或审批流程。

## 二、任务产物

每个新完成的 Coding TaskSession 会生成：

```text
.task_sessions/<task-id>/
  TASK_LOG.md
  CONCLUSIONS.json
  memory/
    SELF.md
    MEMORY.md
    NOW.md
    PENDING.md
    HISTORY.md
    RECENT_CONTEXT.md
```

### `TASK_LOG.md`

保存详细审计信息：

```text
任务 ID
父会话 ID
任务类型
状态
开始和更新时间
用户原始请求
最终回复
结论提取状态
提升、跳过和拒绝数量
完整 TaskSession transcript
工具参数、输出和 hook trace
```

日志只保存在 TaskSession 目录，不会自动进入全局记忆上下文。

### `CONCLUSIONS.json`

保存结构化结论处理结果：

```json
{
  "task_id": "coding-12345678",
  "parent_session_id": "web:default",
  "summary": "任务的一句话摘要",
  "extraction_error": "",
  "raw_response": "模型返回的原始 JSON 文本",
  "llm_candidates": [],
  "promoted": [],
  "skipped": [],
  "rejected": []
}
```

该文件用于复盘 LLM 提取结果和规则过滤结果。

## 三、候选结论来源

### 1. Agent 显式记录

`memorize` 工具的 `section` 新增：

```text
pending
```

TaskSession prompt 会提醒 Coding Agent：发现可复用项目结论时，调用：

```text
memorize(
  section="pending",
  content="项目测试使用 pytest"
)
```

由于 TaskSession memory scope 已经隔离，这条记录首先写入：

```text
.task_sessions/<task-id>/memory/PENDING.md
```

任务结束后，再经过规则层提升到全局 `memory/PENDING.md`。

### 2. LLM 任务结束提取

Coding task 完成后，`TaskConclusionExtractor` 会额外调用一次模型，输入：

```text
用户原始任务
最终任务回复
工具调用摘要
```

模型需要返回 JSON：

```json
{
  "summary": "一句话任务摘要",
  "conclusions": [
    {
      "category": "project",
      "content": "文件上传目录为 storage/",
      "evidence": "docker-compose.yml",
      "confidence": 0.95
    }
  ]
}
```

LLM 只提出候选，不会直接修改 `MEMORY.md`。

## 四、规则过滤

规则层位于：

```text
tasksessions/promotion.py
```

LLM 适合整理语义，规则适合做稳定边界控制。本次两者同时使用。

### 允许的分类

```text
project
decision
preference
fact
task
```

其中 `task` 用于 Agent 显式写入的局部 Pending。

### 拒绝条件

满足任意条件时，候选不会进入全局 Pending：

```text
LLM confidence < 0.65
内容超过 360 个字符
内容超过 4 行
分类不在白名单
内容为空
包含 TaskSession 包装标签
包含 global-memory-snapshot 标签
包含 Task recent context / Task summary
包含 latest_user / latest_assistant
以 session / mode / source_ref 元数据开头
```

### 去重

候选在提升前会使用已有的：

```text
memory.dedup.normalize_memory_text(...)
```

进行规范化精确去重。写入全局 Pending 时，`MemoryStore.append_pending(...)`
还会再次检查全局 `MEMORY.md` 和 `PENDING.md`。

本次没有引入 embedding 或语义相似度依赖。

## 五、TaskSession 专用生命周期

新增：

```text
tasksessions/memory_lifecycle.py
```

`TaskMemoryLifecycle` 继承普通 `MemoryLifecycle`，但关闭基于用户文本关键词的自动候选提取：

```python
def _extract_explicit_memory(self, text: str) -> str:
    return ""

def _extract_candidate(self, text: str) -> str:
    return ""
```

原因是 TaskSession 的首条 user message 包含：

```text
<task-session>
<global-memory-snapshot>
User coding task:
```

普通生命周期可能因为快照中出现“我希望”“以后”“代码风格”等关键词，将整段包装文本
误识别为长期记忆候选。

TaskSession 仍然会保存局部 `HISTORY.md` 和 `RECENT_CONTEXT.md`，只是它们不会自动提升。

## 六、LLM 提取失败时的行为

结论提取属于增强能力，不应阻断 coding task。

如果模型请求失败、超时或返回非法 JSON：

```text
任务回复继续返回给用户
Agent 显式写入的局部 Pending 继续参与提升
错误写入 CONCLUSIONS.json
错误写入 TaskSession metadata
TASK_LOG.md 仍然生成
```

因此，外部模型偶发异常不会导致已完成任务丢失。

## 七、Provider 兼容改动

修改：

```text
core/provider.py
```

普通 Coding Pipeline 仍然发送工具 schema。

结论提取调用不需要工具，因此 `tools=[]` 时 Provider 不再向模型 API 发送空的
`tools` 和 `tool_choice` 字段。这样可以兼容不接受空工具列表的 OpenAI-compatible
接口。

## 八、涉及文件

新增：

```text
tasksessions/artifacts.py
tasksessions/conclusions.py
tasksessions/memory_lifecycle.py
tests/test_task_memory_promotion.py
docs/TASK_MEMORY_HYBRID_PROMOTION.md
```

修改：

```text
core/provider.py
tasksessions/promotion.py
tasksessions/runner.py
tools/schema.py
```

## 九、测试覆盖

新增测试覆盖：

```text
TaskSession 包装 prompt 不会自动进入局部 Pending
显式候选可以提升
包装文本会被规则拒绝
低置信度 LLM 候选会被拒绝
重复候选会被去重
LLM fenced JSON 可以解析
TASK_LOG.md 可以生成
CONCLUSIONS.json 可以生成
无工具的总结请求不会发送空 tools 字段
TaskSessionRunner 可以完整跑通执行、提取、过滤、提升和日志落盘
```

完整验证命令：

```bash
python3 -B -m unittest discover -s tests -v
python3 -B -m py_compile \
  core/provider.py \
  tasksessions/memory_lifecycle.py \
  tasksessions/conclusions.py \
  tasksessions/promotion.py \
  tasksessions/artifacts.py \
  tasksessions/runner.py \
  tools/schema.py
git diff --check
```

## 十、已有数据说明

本次改动只影响后续新任务。

历史 `memory/PENDING.md` 中已经存在的长文本噪音不会被自动删除，以免误删用户数据。
后续可以单独增加：

```text
/memory pending
/memory approve <id>
/memory reject <id>
/memory cleanup
```

用于人工审核和清理历史候选。
