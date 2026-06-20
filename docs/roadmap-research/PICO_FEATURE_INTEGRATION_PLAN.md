# Pico Feature Integration Plan

## 目标

把 pico 项目中最有展示价值的能力迁移到 taleclaw 中，但不照搬 pico 的 runtime。迁移重点是让 taleclaw 更可测评、更可解释、更容易向老师或面试官展示。

pico 的强项是：

- 任务边界清晰。
- 每次运行都有 trace/report/artifacts。
- 上下文预算明确。
- 工具边界、工作区 diff、安全事件记录清楚。
- 有 benchmark 和指标，可以证明 agent 能力。

taleclaw 的强项是：

- Web/CLI/Telegram/Feishu 多入口。
- 多用户隔离。
- 插件系统。
- scheduler 自动任务。
- model provider pool。
- coding task session、teammate、reflection agent。
- storage 和文件管理。

集成方向不是把 taleclaw 改成 pico，而是把 pico 的“可评测、可追踪、可报告”能力变成 taleclaw 的一层工程基础设施。

## 总体原则

### 不迁移 pico runtime

pico 是一个聚焦 CLI coding agent 的轻量 runtime。taleclaw 已经有自己的：

```text
AgentLoop
Pipeline
AgentRunner
ReasoningLoop
ToolExecutor
PluginManager
SessionManager
MemoryLifecycle
```

所以不建议复制 pico 的主循环。

### 迁移 pico 的工程能力

优先迁移这些“能力模块”：

```text
1. Run artifact
2. Trace JSONL
3. Task report
4. Benchmark harness
5. Context budget report
6. Workspace snapshot/diff
7. Tool safety event log
8. Evaluation metrics
```

这些能力能直接补 taleclaw 的短板。

## 第一部分：Run Artifact 系统

### 当前问题

taleclaw 现在有 session、memory、tool trace、scheduler report 等数据，但它们分散在不同模块：

```text
sessions SQLite
memory/*.md/json
storage/
scheduler reports
tool trace
logs
```

这导致演示时很难说清楚：

```text
某一次任务到底经历了什么？
调用了哪些工具？
用了哪些上下文？
产出了什么文件？
结果是否成功？
```

### 目标能力

每次重要运行都生成一个统一目录：

```text
.runs/
  20260608-153000-code-review-8f31/
    run.json
    trace.jsonl
    context_report.json
    tool_events.jsonl
    workspace_diff.json
    artifacts.json
    report.md
```

### run.json

记录运行元信息：

```json
{
  "run_id": "20260608-153000-code-review-8f31",
  "session_id": "web:admin:...",
  "user_id": "admin",
  "persona_id": "security_review",
  "mode": "coding",
  "entrypoint": "web",
  "started_at": "2026-06-08T15:30:00+08:00",
  "ended_at": "2026-06-08T15:30:42+08:00",
  "status": "success",
  "model_purpose": "coding",
  "provider": "deepseek",
  "model": "..."
}
```

### trace.jsonl

记录 agent 推理过程中的关键事件，不保存过长模型原文：

```json
{"type":"user_message","at":"...","text_preview":"帮我检查这段代码"}
{"type":"model_request","at":"...","model_purpose":"coding","message_count":8}
{"type":"tool_call","at":"...","tool":"security_knowledge_search","args_preview":"..."}
{"type":"tool_result","at":"...","tool":"security_knowledge_search","status":"success","result_preview":"..."}
{"type":"assistant_message","at":"...","text_preview":"发现 3 个安全风险"}
```

### report.md

面向用户和面试展示：

```markdown
# Run Report

## Summary

- Status:
- Mode:
- Persona:
- Tools:
- Artifacts:

## User Request

...

## Agent Outcome

...

## Tool Calls

...

## Safety Events

...

## Generated Files

...
```

## 第二部分：Context Budget Report

### pico 的优点

pico 对上下文有明显的 section 预算：

```text
prefix
memory
relevant_memory
history
current_request
workspace
```

这让它很容易解释：

```text
为什么这次 prompt 没爆？
哪些上下文被压缩了？
哪些内容被裁掉了？
```

### taleclaw 的迁移方案

在 `ContextBuilder` 或 `Pipeline._before_reasoning` 后增加 context report：

```json
{
  "total_chars": 18320,
  "sections": [
    {
      "name": "system_prompt",
      "chars": 2400,
      "included": true
    },
    {
      "name": "recent_context",
      "chars": 3600,
      "included": true
    },
    {
      "name": "memory",
      "chars": 1200,
      "included": true
    },
    {
      "name": "task_runtime_events",
      "chars": 900,
      "included": false,
      "reason": "not coding task session"
    }
  ],
  "compact": {
    "micro_compact_used": true,
    "auto_compact_used": false
  }
}
```

### 价值

- 展示 taleclaw 的上下文工程能力。
- 方便 debug 为什么 agent 忘东西。
- 和 memory lifecycle 形成闭环。

## 第三部分：Workspace Snapshot 和 Diff

### 当前问题

taleclaw 可以写文件、上传文件、生成报告，但缺少“这次任务改了什么”的统一说明。

### 目标能力

每次 coding task 开始前记录 workspace snapshot：

```json
{
  "files": [
    {
      "path": "core/pipeline.py",
      "sha256": "...",
      "size": 12345,
      "mtime": "..."
    }
  ]
}
```

任务结束后生成 diff：

```json
{
  "created": ["docs/SECURITY_REVIEW_RAG_PERSONA_PLAN.md"],
  "modified": ["plugins/security_review/plugin.py"],
  "deleted": [],
  "large_changes": []
}
```

### 接入点

可以在 `AgentRunner` 外层新增：

```text
RunRecorder.before_run()
RunRecorder.after_run()
```

对于 web/bot 普通聊天可以不启用 diff；对 coding、scheduled_agent、security_review_file 启用。

## 第四部分：Benchmark Harness

### 当前问题

taleclaw 功能很多，但可测评性弱：

```text
这个 agent 到底能不能稳定完成任务？
代码修复成功率是多少？
工具调用是否越界？
记忆是否生效？
scheduler 是否能自动生成报告？
```

### 目标能力

新增：

```text
evaluation/
  task_schema.py
  harness.py
  runner.py
  metrics.py
  report.py
  verifiers.py

benchmarks/
  coding_tasks.json
  memory_tasks.json
  security_review_tasks.json
  scheduler_tasks.json

scripts/
  run_evals.py
```

### task schema

```json
{
  "id": "security-flask-login-001",
  "category": "security_review",
  "mode": "bot",
  "persona_id": "security_review",
  "prompt": "检查 fixtures/security/flask_login.py 的安全问题",
  "fixture": "fixtures/security/flask_login.py",
  "allowed_tools": ["read_file", "security_knowledge_search", "security_review_file"],
  "max_reasoning_steps": 8,
  "expected": {
    "mentions": ["SQL 注入", "CWE-89", "参数化查询"],
    "must_cite_sources": true
  },
  "verifier": "security_review_contains"
}
```

### verifier 类型

第一版实现这些 verifier：

| verifier | 用途 |
| --- | --- |
| `text_contains` | 检查回答是否包含关键词 |
| `file_exists` | 检查是否生成目标文件 |
| `file_contains` | 检查生成文件内容 |
| `tool_called` | 检查是否调用某工具 |
| `unsafe_tool_blocked` | 检查危险工具是否被阻止 |
| `citation_present` | 检查是否带引用 |
| `security_review_contains` | 检查安全审查结果 |

### metrics

```json
{
  "pass_rate": 0.83,
  "avg_reasoning_steps": 4.2,
  "avg_tool_calls": 2.1,
  "unsafe_tool_block_rate": 1.0,
  "citation_rate": 0.9,
  "memory_hit_rate": 0.75,
  "artifact_success_rate": 0.88
}
```

### 输出目录

```text
.evals/
  runs/
    20260608-160000/
      task_results.jsonl
      summary.json
      summary.md
      artifacts/
```

## 第五部分：固定 Benchmark 集合

### coding_tasks

建议 4 个：

```text
1. 修复一个 Python 函数 bug
2. 新增一个小工具函数和测试
3. 修改前端按钮文案
4. 根据错误日志定位问题
```

### memory_tasks

建议 3 个：

```text
1. 用户显式要求记住偏好
2. 多轮对话后检查 recent context
3. 老 recent 被归档后能否检索
```

### security_review_tasks

建议 4 个：

```text
1. Flask SQL 注入审查
2. 路径穿越审查
3. 硬编码密钥审查
4. 明文密码存储审查
```

### scheduler_tasks

建议 3 个：

```text
1. 创建日报任务
2. 审批后自动执行
3. 生成报告并推送 Telegram/Feishu outbox
```

第一版总数控制在 12 到 16 个任务，方便跑完和展示。

## 第六部分：Tool Safety Event Log

### pico 的优点

pico 对工具执行边界记录清楚：

```text
工具是否允许
参数是否合法
是否需要审批
是否重复调用
是否修改 workspace
```

### taleclaw 的迁移方案

现有 taleclaw 已经有：

```text
ToolExecutor
FileWriteScopeHook
ShellSafetyPlugin
ToolLoopGuardHook
ToolTraceHook
```

需要补的是统一安全事件输出：

```json
{
  "type": "tool_safety_event",
  "tool": "bash",
  "decision": "blocked",
  "reason": "command attempts to write outside sandbox",
  "session_id": "...",
  "run_id": "...",
  "at": "..."
}
```

这些事件写入：

```text
.runs/<run_id>/tool_events.jsonl
```

## 第七部分：Delegate / Teammate 能力展示

### pico 的相关能力

pico 有 delegate child agent，用于子任务。

### taleclaw 当前状态

taleclaw 已有：

```text
coding_runtime/teammate.py
TaskSessionRunner
AgentRunner
ReflectionAgent
MessageBus
```

所以不需要照搬 pico 的 delegate。更合理的是把 teammate 能力包装成可观测的 run：

```text
主 agent 创建任务
teammate 执行子任务
teammate 生成 run report
主 agent 汇总 teammate 结论
```

### 需要补充

- teammate run id。
- teammate trace。
- teammate result summary。
- teammate artifacts。
- teammate safety events。

## 第八部分：Structured Memory Topics

### pico 的优点

pico 的 memory 更像有主题的结构化资料：

```text
project-conventions
key-decisions
dependency-facts
user-preferences
```

### taleclaw 迁移方案

taleclaw 现在有：

```text
memory
pending
history
recent_context
archive
```

可以增加 topic 层：

```text
memory/topics/
  user-preferences.md
  project-conventions.md
  key-decisions.md
  dependency-facts.md
  security-findings.md
```

或者结构化 JSON：

```json
{
  "topic": "project-conventions",
  "items": [
    {
      "text": "前端使用原生 HTML/CSS/JS，不引入 React。",
      "source_ref": "...",
      "confidence": 0.8,
      "updated_at": "..."
    }
  ]
}
```

第一版建议只加 report，不急着改 memory 存储。

## 推荐实施路线

### Phase 1：RunRecorder

新增：

```text
core/run_recorder.py
```

职责：

```text
create_run()
record_event()
record_tool_call()
record_context_report()
record_artifact()
finish_run()
write_report()
```

接入：

```text
Pipeline
ReasoningLoop
ToolExecutor 或 ToolTraceHook
```

验收：

```text
任意一次 coding 任务结束后生成 .runs/<run_id>/report.md
```

### Phase 2：Context Report

接入 `ContextBuilder` 输出 context section 统计。

验收：

```text
report.md 中能看到本次 prompt 使用了哪些上下文。
```

### Phase 3：Benchmark Harness

新增 evaluation 模块和 6 个基础任务。

验收：

```bash
python scripts/run_evals.py --suite smoke
```

输出：

```text
.evals/runs/<run_id>/summary.md
```

### Phase 4：Security Review Benchmark

接入安全 RAG 后，新增安全审查 benchmark。

验收：

```text
Flask SQL 注入样例能通过 verifier。
```

### Phase 5：Teammate / Scheduled Agent Run Report

让 teammate 和 scheduled_agent 也产出 run artifact。

验收：

```text
自动日报任务执行后，有 scheduler run report 和 outbox 推送记录。
```

## 最小可行实现

如果只做一天，建议只做：

```text
1. core/run_recorder.py
2. .runs/<run_id>/trace.jsonl
3. .runs/<run_id>/report.md
4. evaluation/verifiers.py
5. benchmarks/security_review_tasks.json
```

这已经能显著提升项目可展示性。

## 和安全 RAG 的组合演示

最佳演示路线：

```text
1. 用户让 coding agent 生成一个 Flask 登录接口
2. 切换 security_review persona
3. security_review_file 审查代码
4. agent 检索 OWASP/CWE
5. 输出带引用的安全报告
6. RunRecorder 生成本次任务报告
7. Benchmark harness 验证报告包含 SQL 注入、CWE-89、参数化查询
```

这条链路能同时展示：

- coding agent。
- persona 隔离。
- RAG 检索。
- 工具调用。
- 引用溯源。
- 安全审查。
- 运行 trace。
- benchmark 评测。

## 面试叙事

可以这样讲：

```text
我参考了一个轻量 coding agent 项目 pico 的工程设计。
pico 的主循环很简洁，但它做得最好的是可观测性和可评测性。
我的项目 taleclaw 更像一个多入口、多插件、多用户的 agent 平台，所以我没有迁移它的 runtime，而是迁移它的 run artifact、trace、report、benchmark、context budget 等能力。
这样 taleclaw 不只是功能多，还能证明每个功能是否稳定完成。
```

## 不建议迁移的内容

### 不建议复制 pico 的 agent loop

taleclaw 已经有 ReasoningLoop 和 AgentRunner。复制会造成两套主循环。

### 不建议复制 pico 的工具系统

taleclaw 已经有 ToolRegistry、ToolExecutor、hooks、plugins。应当增强现有系统，而不是替换。

### 不建议复制 pico 的 memory 存储

taleclaw 已经有 history、recent、pending、archive 和 user scope。可以吸收 topic memory 的思想，但不要直接搬存储结构。

## 成功标准

完成后，taleclaw 应该可以回答这几个问题：

```text
这次 agent 任务做了什么？
用了哪些上下文？
调用了哪些工具？
有没有工具安全事件？
改了哪些文件？
产出了哪些 artifact？
任务是否通过 verifier？
和上一次评测相比有没有退步？
```

如果这些问题能回答清楚，taleclaw 的可测评性和项目说服力会明显提升。
