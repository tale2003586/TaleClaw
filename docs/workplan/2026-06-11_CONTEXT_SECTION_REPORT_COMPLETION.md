# Context Section Report 第一阶段完成记录

日期：2026-06-11

## 任务背景

当前 `ContextBuilder` 已经能拼装 system prompt、按模式加载 instructions、注入 memory、inbox 和 background results。

但它缺少一份结构化说明：每轮上下文里到底有哪些来源、各自占了多少字符、哪些文件被截断、最终 messages 总体规模是多少。

本次改动目标是先完成上下文管理第一阶段：只做可观测的 section report，不改变模型实际可见内容，为后续 section budget、history 压缩和 relevant memory 打基础。

## 改动范围

新增文件：

- `runtime/context_sections.py`
- `docs/workplan/2026-06-11_CONTEXT_SECTION_REPORT_COMPLETION.md`

修改文件：

- `runtime/context.py`
- `runtime/reasoning_loop.py`
- `tests/test_context_instructions.py`
- `docs/system-design/05-上下文构建、压缩与记忆生命周期.md`

## 核心实现

### 1. 新增 ContextSection

`ContextSection` 记录单个上下文来源：

- `name`
- `raw_chars`
- `rendered_chars`
- `budget_chars`
- `truncated`
- `metadata`

### 2. 新增 ContextBuildReport

`ContextBuildReport` 记录一次上下文构建：

- `total_chars`
- `budget_chars`
- `over_budget`
- `sections`
- `reductions`
- `metadata`

当前第一阶段不做裁剪，所以 `budget_chars` 和 `reductions` 主要是为下一阶段预留。

### 3. ContextBundle 带 report

`ContextBundle` 从：

```python
ContextBundle(messages=list[dict])
```

扩展为：

```python
ContextBundle(
    messages=list[dict],
    report=ContextBuildReport | None,
)
```

现有只读取 `.messages` 的调用方不受影响。

### 4. ContextBuilder 记录 section

当前记录的逻辑 section 包括：

- `system_profile`
- `mode_instructions`
- `project_instructions`
- `runtime_guidance`
- `system_prompt`
- `conversation_history`
- `current_request`
- `memory`
- `task_runtime_events`
- `inbox`
- `background_results`
- `context_frame`

instruction 文件如果被截断，会在对应逻辑 section 中标记 `truncated=true`。具体文件来源放在 section metadata 的 `sources` 和 `files` 里。

### 5. 逻辑 section 与 chat message 分离

本次明确了一个装配原则：逻辑层细，传输层简。

逻辑 section 用于 trace、预算和后续裁剪：

```text
system_profile
mode_instructions
project_instructions
conversation_history
current_request
memory
task_runtime_events
```

实际发给 provider 的 message 结构仍然保持：

```text
system message
session history messages
optional context frame user message
```

这样后续可以继续做细粒度预算，不会把 provider 侧消息结构改得过于复杂。

### 6. Trace 接入 context_report

`ReasoningLoop` 的 `context.build.completed` 事件现在会带：

```json
"context_report": {...}
```

如果某些测试或特殊 builder 只返回 `messages`，没有 `report`，这里会自动写空对象，兼容旧路径。

## 验证方式

运行新增 context instruction 测试：

```bash
python -m unittest discover -s tests -p 'test_context_instructions.py' -v
```

结果：

```text
Ran 3 tests
OK
```

运行 run trace 回归测试：

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

下一阶段可以在 `ContextBuildReport` 上继续加：

- `CONTEXT_BUDGET_CHARS`
- section budgets
- section floors
- reduction order
- history/tool result compaction
- relevant memory top-k
- benchmark 中的 context 指标聚合

推荐下一步先做 section budget，但仍然只用字符预算，不急着引入 tokenizer。
