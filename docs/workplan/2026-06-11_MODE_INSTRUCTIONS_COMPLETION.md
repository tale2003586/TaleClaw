# 按模式拆分 Assistant / Coding Instructions 完成记录

日期：2026-06-11

## 任务背景

聊天助手和 coding agent 需要不同的长期行为说明。

如果把所有规则都写进同一份 memory 或同一段 system prompt，会产生两个问题：

- 普通聊天会被 coding 规则污染。
- coding agent 的工程约束不够明确，容易退化成普通聊天助手。

因此本次改动把长期 instructions 拆成两份：

- 主线聊天模式使用 `.agent/assistant.md`
- coding agent 使用 `.agent/coding.md`

同时 coding 模式额外兼容项目级 `AGENTS.md`。

## 改动范围

新增文件：

- `.agent/assistant.md`
- `.agent/coding.md`
- `tests/test_context_instructions.py`
- `docs/workplan/2026-06-11_MODE_INSTRUCTIONS_COMPLETION.md`

修改文件：

- `runtime/context.py`
- `docs/system-design/05-上下文构建、压缩与记忆生命周期.md`

## 核心实现

`ContextBuilder` 新增 instruction 文件加载能力。

聊天 / bot 模式加载：

```text
.agent/assistant.md
```

coding 模式加载：

```text
.agent/coding.md
AGENTS.md
```

读取到的内容会注入到 system prompt：

```text
<instructions source=".agent/coding.md">
...
</instructions>
```

同时为 instruction 文件设置了长度上限，避免单个规则文件过大导致上下文膨胀。

## 两份 instruction 的职责

`.agent/assistant.md` 放主线聊天助手规则：

- 默认中文。
- 先给结论，再给必要原因。
- 不空泛鼓励。
- 不编造项目状态。
- 不暴露 `.env`、token、密码等敏感信息。
- memory 适合放用户偏好，不适合承载硬安全约束。

`.agent/coding.md` 放 coding agent 工程规则：

- 修改前先读相关代码和测试。
- 优先使用 `rg`。
- 保持改动聚焦。
- 只能在当前 workspace 内工作。
- 大文件先搜索、摘要或分段读取。
- `git_add`、`git_commit` 只有用户明确要求时使用。
- 完成后说明改动和验证。

## 验证方式

新增测试：

```bash
python -m unittest discover -s tests -p 'test_context_instructions.py' -v
```

结果：

```text
Ran 2 tests
OK
```

回归多用户上下文测试：

```bash
python -m unittest discover -s tests -p 'test_multi_user_isolation.py' -v
```

结果：

```text
Ran 7 tests
OK
```

语法检查：

```bash
python -m py_compile runtime/context.py
```

通过。

## 后续建议

- 后续可以把 instruction loader 从 `ContextBuilder` 中拆成独立模块。
- 支持 workspace 子目录级 `AGENTS.md`，类似 Codex 的分层发现。
- 把 instruction section 的 token / 字符数写入 `context.build.completed` trace。
- 为 explore / review / teammate 增加独立 `.agent/*.md`。

