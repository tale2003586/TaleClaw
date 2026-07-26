# Project Structure

TaleClaw 按 Core Runtime、Applications、Agent Definitions 和 Optional
Extensions 组织。生产代码不再使用 Mode、Pipeline 或兼容 shim。

## 主结构

```text
applications/
  coding/
    runner.py              # CodingApplication 生命周期
    session.py             # 隔离 Coding Session
    context_state.py
    handoff.py
    artifacts.py
    conclusions.py
    promotion.py
    memory_lifecycle.py
    orchestration/         # task board、teammate、background task

agents/
  definitions.py           # Bot/Coding AgentSpec
  subagent/                # Child Run 与 Subagent result/protocol

runtime/
  runtime.py               # 唯一 Runtime.run() 入口和 turn execution
  agent_loop.py
  context/                 # Builder、Provider、Budget、History、Report
  execution/               # AgentRunner、ReasoningLoop、Policy、Failure
  tooling/                 # Tool result 压缩、存储与签名
  routing/                 # AgentRouter、ExecutionPlan、Intent
  messaging/               # user/team message buses
  sessions/                # Session 与 PostgreSQL SessionStore
  trace/                   # 可选 Trace

models/                    # Provider、ModelPool、ModelTaskRunner
tools/                     # Schema、Authorization、Executor、Hooks
memory/                    # Memory 扩展与 Background lifecycle
retrieval/ + knowledge/    # Security RAG
plugins/                   # 可选产品插件
gateway/ + web/            # 外部入口
evaluation/                # Evaluation 与 SWE-bench adapter
tests/                     # 行为、兼容、安全和回归测试
```

## 运行链路

```text
Gateway / CLI / Web
→ AppRuntime
→ runtime.messaging
→ AgentLoop
→ AgentRouter
→ Runtime.run(AgentSpec, input, RunContext)
→ runtime.execution.AgentRunner
→ runtime.execution.ReasoningLoop
→ ToolExecutor / ModelProvider
```

Coding 请求进入 `applications.coding.CodingApplication`，由它管理 workspace、
隔离 Session、handoff、artifact、结论和 Memory promotion；通用 Runtime 不承担
这些应用职责。

## 依赖方向

```text
gateway/web → runtime + applications
applications → runtime + extensions
agents → runtime contracts
runtime → models/tools
extensions → runtime contracts
```

`runtime` 不依赖 Gateway；`agents` 不持有 Session 状态；Optional Extensions
不成为 `Runtime.run()` 的强制依赖。
