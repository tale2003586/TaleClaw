# Context 逻辑分层与消息装配完成记录

日期：2026-06-11

## 任务背景

第一阶段已经让 `ContextBuilder` 输出 `ContextBuildReport`，但 report 里的 instruction section 仍偏文件级，例如 `instructions:.agent/coding.md`。

这不利于后续做预算裁剪。预算应该面向逻辑层，而不是具体文件名。例如 `.agent/coding.md` 和未来 `.agent/review.md` 都属于 mode instructions；`AGENTS.md` 属于 project instructions。

本次改动目标是明确两层结构：

- 逻辑 section：给 runtime、trace、budgeter 使用。
- chat messages：给模型 provider 使用。

## 改动范围

修改文件：

- `runtime/context.py`
- `tests/test_context_instructions.py`
- `docs/system-design/05-上下文构建、压缩与记忆生命周期.md`
- `docs/workplan/2026-06-11_CONTEXT_SECTION_REPORT_COMPLETION.md`

新增文件：

- `docs/workplan/2026-06-11_CONTEXT_LOGICAL_SECTION_ASSEMBLY_COMPLETION.md`

## 核心实现

### 1. instruction section 聚合

原来 report 顶层 section 是：

```text
instructions:.agent/assistant.md
instructions:.agent/coding.md
instructions:AGENTS.md
```

现在改成：

```text
mode_instructions
project_instructions
```

具体来源文件进入 metadata：

```json
{
  "sources": [".agent/coding.md"],
  "files": [
    {
      "source": ".agent/coding.md",
      "raw_chars": 1000,
      "rendered_chars": 1000,
      "truncated": false
    }
  ]
}
```

### 2. current_request 从 history 中单独标记

`ContextBuilder` 现在会在 report 层识别 session 中最新的 user message：

```text
current_request
```

同时 `conversation_history` 只统计最新 user message 之前的历史。

注意：这只是 report 层分离。实际 messages 中不会重复注入 current request，避免模型看到两份同样的用户请求。

### 3. task_runtime_events 聚合

`inbox` 和 `background_results` 继续保留细节 section，同时新增逻辑聚合层：

```text
task_runtime_events
```

它用于后续统一预算和裁剪 task session 的临时运行事件。

### 4. 保持 provider 传输结构简单

实际发送给模型的 messages 仍保持：

```python
messages = [
    {"role": "system", "content": system_prompt},
    *session.messages,
    optional_context_frame,
]
```

也就是说，一个 chat message 可以由多个逻辑 section 合成；一个逻辑 section 也可以对应多条 chat messages。

## 验证方式

运行 context 测试：

```bash
python -m unittest discover -s tests -p 'test_context_instructions.py' -v
```

结果：

```text
Ran 4 tests
OK
```

运行 trace 回归测试：

```bash
python -m unittest discover -s tests -p 'test_run_trace.py' -v
```

结果：

```text
Ran 9 tests
OK
```

语法检查：

```bash
python -m py_compile runtime/context.py runtime/context_sections.py runtime/reasoning_loop.py
```

通过。

## 后续建议

下一步可以直接基于这些逻辑 section 做预算裁剪：

- `mode_instructions`
- `project_instructions`
- `conversation_history`
- `current_request`
- `memory`
- `task_runtime_events`

建议永不裁剪 `current_request`，优先裁剪 `task_runtime_events` 和 `conversation_history`。
