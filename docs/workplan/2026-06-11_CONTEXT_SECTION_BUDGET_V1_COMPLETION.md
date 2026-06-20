# Context Section Budget V1 完成记录

日期：2026-06-11

## 背景

之前上下文构建已经拆出了逻辑 section，并能在 trace 中看到每个 section 的 raw chars、rendered chars 和 metadata。

这次补上第一版预算裁减能力，让系统在上下文过大前先裁剪低风险内容，而不是等 provider 报错。

## 本次完成

新增 `runtime/context_budget.py`：

- `ContextBudgeter`
- `SectionBudgetRule`
- `BudgetedText`

`ContextBuilder` 现在会在装配 messages 前裁剪这些 section：

- `mode_instructions`
- `project_instructions`
- `memory`
- `task_runtime_events`

不会裁剪这些内容：

- `session.messages`
- `conversation_history`
- `current_request`

## 默认策略

默认关闭：

```text
CONTEXT_ENABLE_SECTION_BUDGET=0
```

开启后默认预算：

```text
CONTEXT_BUDGET_CHARS=24000
CONTEXT_MODE_INSTRUCTIONS_BUDGET=3000
CONTEXT_PROJECT_INSTRUCTIONS_BUDGET=3000
CONTEXT_MEMORY_BUDGET=2500
CONTEXT_TASK_RUNTIME_EVENTS_BUDGET=2000
```

裁剪策略：

- `mode_instructions`：head
- `project_instructions`：head
- `memory`：head_tail
- `task_runtime_events`：tail

## 可观测性

触发裁剪后，`ContextBuildReport.reductions` 会记录：

- section 名称
- 裁剪原因
- 裁剪前后字符数
- 预算值
- floor 值
- 实际生效预算
- 裁剪策略

对应 section 的 `truncated` 字段也会变为 `true`。

## 配置入口

`.env.example` 已补充：

```text
CONTEXT_ENABLE_SECTION_BUDGET=0
CONTEXT_BUDGET_CHARS=24000
CONTEXT_MODE_INSTRUCTIONS_BUDGET=3000
CONTEXT_PROJECT_INSTRUCTIONS_BUDGET=3000
CONTEXT_MEMORY_BUDGET=2500
CONTEXT_TASK_RUNTIME_EVENTS_BUDGET=2000
CONTEXT_MODE_INSTRUCTIONS_FLOOR=1000
CONTEXT_PROJECT_INSTRUCTIONS_FLOOR=1000
CONTEXT_MEMORY_FLOOR=500
CONTEXT_TASK_RUNTIME_EVENTS_FLOOR=300
```

## 测试

已新增覆盖：

- section budget 开启后会裁剪 instruction、memory、task runtime events。
- 最新用户请求保持完整。
- reductions 中能看到所有触发裁剪的 section。

已执行：

```bash
python -m unittest discover -s tests -p 'test_context_instructions.py' -v
python -m unittest discover -s tests -p 'test_run_trace.py' -v
python -m py_compile runtime/context.py runtime/context_budget.py runtime/context_sections.py runtime/reasoning_loop.py
```

结果均通过。

## 后续建议

V1 仍然是 char budget，不是 tokenizer 级预算。

下一步可以做：

- 根据当前 route model 自动选择 token window。
- 将 char budget 替换为 tokenizer estimate。
- 给 `conversation_history` 增加摘要式裁剪，但必须保留最新 N 轮和当前请求。
- 在 trace viewer 里突出显示 context reductions。
