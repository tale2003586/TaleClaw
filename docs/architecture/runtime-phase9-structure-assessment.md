# TaleClaw Phase 9：Runtime 内部目录结构评估

日期：2026-07-23
阶段状态：设计已批准并实施，等待最终代码审核

## 1. 结论

`runtime/` 的问题不是文件数量本身，而是三个不同层次同时平铺：

1. 稳定入口和契约，如 `runtime.py`、`agent_spec.py`、`bootstrap.py`；
2. 大型实现子域，如 Context 和 Reasoning/Execution；
3. 底层辅助能力，如 Tool Result、DB、环境加载。

Phase 8 已经把 Messaging、Routing、Sessions 和 Trace 收敛为目录，但顶层仍有
28 个 Python 源文件（包含 `__init__.py`）。建议 Phase 9 只收敛边界已经足够
稳定的三个子域：

- `context/`
- `execution/`
- `tooling/`

不建议为了追求“顶层文件少”而给单个小模块创建目录。

## 2. 规模证据

| 模块 | 行数 | 判断 |
|---|---:|---|
| `reasoning_loop.py` | 1983 | Execution 核心，职责和辅助函数过多 |
| `context.py` | 1776 | Context 主编排，已形成独立子系统 |
| `working_memory.py` | 1177 | 需要后续逻辑拆分，单纯移动收益有限 |
| `context_history.py` | 755 | Context 子域 |
| `tool_result_compression.py` | 502 | Tooling 子域 |
| `runtime.py` | 484 | 对外 Facade，应保留顶层 |
| `agent_loop.py` | 441 | App/消息入口编排，应保留顶层 |
| `token_estimator.py` | 341 | Context 与模型调用共享 |
| `tool_result_store.py` | 319 | Tooling 子域 |
| `context_budget.py` | 277 | Context 子域 |
| `loop_policies.py` | 253 | Execution 子域 |
| `context_providers.py` | 206 | Context 子域 |

## 3. 建议目标结构

```text
runtime/
  __init__.py

  # 稳定入口、契约与组合根
  runtime.py
  app_runtime.py
  agent_loop.py
  agent_spec.py
  bootstrap.py
  extensions.py

  context/
    __init__.py
    builder.py
    budget.py
    build_state.py
    history.py
    providers.py
    sections.py

  execution/
    __init__.py
    agent_runner.py
    child_run.py
    failure_reasons.py
    loop_policies.py
    reasoning_loop.py
    reflection.py

  tooling/
    __init__.py
    result_compression.py
    result_store.py
    signature.py

  messaging/
  routing/
  sessions/
  trace/
  units/

  # 暂时保留；本阶段不为单文件制造目录
  db.py
  env_loader.py
  logging.py
  token_estimator.py
  working_memory.py
  workspace.py
```

## 4. 已执行移动映射

| 当前路径 | 目标路径 |
|---|---|
| `runtime/context.py` | `runtime/context/builder.py` |
| `runtime/context_budget.py` | `runtime/context/budget.py` |
| `runtime/context_build_state.py` | `runtime/context/build_state.py` |
| `runtime/context_history.py` | `runtime/context/history.py` |
| `runtime/context_providers.py` | `runtime/context/providers.py` |
| `runtime/context_sections.py` | `runtime/context/sections.py` |
| `runtime/agent_runner.py` | `runtime/execution/agent_runner.py` |
| `runtime/child_run.py` | `runtime/execution/child_run.py` |
| `runtime/failure_reasons.py` | `runtime/execution/failure_reasons.py` |
| `runtime/loop_policies.py` | `runtime/execution/loop_policies.py` |
| `runtime/reasoning_loop.py` | `runtime/execution/reasoning_loop.py` |
| `runtime/reflection.py` | `runtime/execution/reflection.py` |
| `runtime/tool_result_compression.py` | `runtime/tooling/result_compression.py` |
| `runtime/tool_result_store.py` | `runtime/tooling/result_store.py` |
| `runtime/tool_signature.py` | `runtime/tooling/signature.py` |

## 5. 为什么这些文件不移动

### 保留 Runtime Facade

`runtime/runtime.py` 定义 `Runtime`、`RunContext`、`RunResult` 和
`RunExecutionState`，是 Phase 2～4 建立的稳定 Facade。把它藏进
`execution/` 会让 Runtime 的公共入口再次变得不清晰。

### 保留 AgentLoop 和 AppRuntime

它们处理用户消息到 Runtime 的应用入口生命周期，并不等同于模型
Reasoning Loop。保留在顶层可以清晰区分：

```text
AppRuntime -> AgentLoop -> Runtime -> Execution internals
```

### 暂不移动 Working Memory

`working_memory.py` 虽然有 1177 行，但目前没有自然的同级模块。给它单独创建
`state/` 或 `memory/` 目录只会产生一层包装，而且容易与顶层 `memory/` 包混淆。
它需要的是后续按 checkpoint、render、normalization 拆分，而不是本阶段的
机械搬运。

### 暂不移动 DB、Env 和 Logging

这些是跨 Gateway、Memory、Web 和脚本使用的基础设施。放入
`runtime/infrastructure/` 会强化错误的所有权；搬到顶层 `infrastructure/`
又超出 Runtime 内部重构范围。因此本阶段保持原位并记录为架构债务。

### 暂不把 Token Estimator 收入 Context

它既服务 Context，也在模型调用前执行安全窗口裁剪。归入 Context 会造成
Execution 对 Context 内部实现的依赖，因此保持为 Runtime 共享能力。

## 6. API 与依赖策略

### Context

把 `context.py` 变为 `context/` package 后，`context/__init__.py` 导出：

```python
from .builder import ContextBuilder, ContextBundle, ContextPrefix
```

现有 `from runtime.context import ContextBuilder` 保持不变，无需兼容 shim。

### Execution 与 Tooling

仓库内部导入统一迁移为 `runtime.execution.*` 和 `runtime.tooling.*`。
`__init__.py` 只导出明确的跨边界类型，避免 eager import 造成循环依赖。
不保留原位置的同名转发文件，因为那会重新制造 Phase 7 已删除的兼容路径。

### 依赖方向

目标方向为：

```text
bootstrap/app_runtime/agent_loop
              |
              v
       runtime.py Facade
              |
              v
         execution/*
          /    |    \
   context  tooling  trace
```

Context 当前直接导入 `applications.coding.context_state`，形成 Core Runtime
反向依赖 Application。实施时必须通过 `CodingContextProvider` 注入回调或扩展
能力解除该依赖；不能把 Coding Context 搬回 Runtime。

## 7. 实施批次

为降低循环依赖和 Diff 审查风险，Phase 9 应在同一阶段内串行执行：

1. 建立 `runtime/context/`，保持 `runtime.context` 公共 API；
2. 迁移 `runtime/tooling/` 并更新直接消费者；
3. 迁移 `runtime/execution/`，保持 Runtime Facade 在顶层；
4. 解除 Context 对 Coding Application 的反向依赖；
5. 更新 tests、benchmarks、packaging 和架构文档；
6. 搜索旧路径并运行完整 Gate。

每一批完成后先运行定向测试，全部完成后再创建一个独立 Phase 9 提交。

## 8. Gate

### 行为 Gate

- Phase 0 Prompt、Tool、Trace、Coding Lifecycle 和 Gateway 快照不变；
- Chat、Coding、Subagent 的消息与停止语义不变。

### 测试 Gate

- Context/History/Budget/Provider 定向测试；
- Runtime/Reasoning/Working Memory 定向测试；
- Tool result store/compression/signature 定向测试；
- 完整测试套件。

### 兼容 Gate

- `from runtime.context import ContextBuilder, ContextBundle` 保持有效；
- Runtime Facade 的公开类型与调用方式不变；
- 仓库内不存在旧 `runtime.context_*`、`runtime.agent_runner`、
  `runtime.reasoning_loop`、`runtime.tool_result_*` 导入；
- 不增加旧路径 shim。

### 性能 Gate

- 使用 Phase 8 相同 benchmark；
- Context token 快照不变；
- Runtime Facade、Chat no-tool、Subagent 无明显退化。

### 安全 Gate

- Tool authorization、workspace containment、Session identity 测试通过；
- Tool result 存储与压缩迁移不改变引用验证和权限路径。

### 文档 Gate

- 项目结构、代码库概览、Phase 9 设计、执行报告和状态同步。

## 9. 审核决策

建议批准该方案。它把顶层 Python 源文件从 28 个减少到 13 个，同时避免：

- 将稳定 Facade 藏入实现目录；
- 为单文件创建无意义目录；
- 重建兼容 shim；
- 混淆 Runtime Working Memory 与顶层持久化 Memory；
- 把 Application 专属 Coding Context 放回 Core。

该方案已获批准并完成实施；最终 Gate 证据见
`runtime-phase9-execution-report.md`。
