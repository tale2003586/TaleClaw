# 当前代码目录总结

TaleClaw 是一个多入口 Agent Runtime 平台。核心执行、产品应用和可选扩展具有
明确物理边界。

## 核心执行

`runtime/runtime.py` 提供唯一公共入口：

```python
Runtime.run(agent: AgentSpec, input: str, context: RunContext) -> RunResult
```

Runtime 负责 turn setup、Context composition、AgentRunner 和 Memory hook；
ReasoningLoop 只负责 Model → Tool → Model 机制。

## 应用

`applications/coding/` 是 CodingApplication：

- workspace 解析与隔离；
- Coding Session；
- Context handoff；
- Working Memory；
- Artifact 与 workspace diff；
- 结论提取和 Memory promotion；
- task/teammate/background orchestration。

这些职责不进入通用 Runtime。

## Agent

`agents/definitions.py` 定义 Bot/Coding AgentSpec。`agents/subagent/` 实现带独立
identity 和 parent linkage 的 Child Run。

## Runtime 子系统

- `runtime/routing/`：AgentSpec 驱动的路由；
- `runtime/messaging/`：入口消息和团队 mailbox；
- `runtime/sessions/`：Session 和 PostgreSQL 存储；
- `runtime/trace/`：可选 Trace；
- `runtime/context/`：Context builder/provider/budget/history/report；
- `runtime/execution/`：AgentRunner、ReasoningLoop、策略和失败语义；
- `runtime/tooling/`：Tool result 压缩、存储与签名。

## 扩展

- `memory/`：长期记忆与后台生命周期；
- `retrieval/`、`knowledge/`：Security RAG；
- `plugins/`：Web Search、报告、PDF、安全等产品插件；
- `tools/`：工具定义、authorization、执行和安全 hook。

## 主要入口

- `cli.py`
- `web/server.py`
- `telegram_worker.py`
- `feishu_worker.py`
- `scheduler_worker.py`

所有入口通过 `runtime.bootstrap.build_runtime()` 使用同一 Runtime 和应用装配。
