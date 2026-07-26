# TaleClaw Runtime Phase 4：显式 RunContext

> 实施日期：2026-07-23

## 目标

将一次 Runtime 执行所需的依赖和临时状态放入显式 `RunContext`，同时保留
`Session.metadata` 双写，避免改变现有 Session、恢复、Streaming 和应用状态语义。

## 边界

```text
AgentSpec
  声明式、可复用的 Agent 配置

RunContext
  Session、callback、Trace、依赖和本次 RunExecutionState

Session / Application State
  跨 Run 的对话历史和应用级持久状态
```

`RunExecutionState` 当前显式承载：

- run/parent run identity；
- 当前输入和消息引用；
- cancellation/limit 产生的 stop reason 与 message；
- Security Knowledge 本 Run 使用状态；
- finishing reminder 状态；
- Web Search budget；
- usage 和扩展 metadata。

## 真实调用链

```text
Runtime.run(agent, input, RunContext)
→ Pipeline.run(..., run_context)
→ AgentRunner.run_turn(..., run_context)
→ ReasoningLoop.run(..., run_context)
→ loop policies read/write RunExecutionState
```

Runtime 在入口记录当前输入和 Session 消息引用。Pipeline 在 turn 开始初始化
本次预算和标记；ReasoningLoop 与 policy 更新显式状态。

## 兼容迁移

Phase 4 不删除旧 metadata key。以下值在迁移期双写：

| 状态 | 显式位置 | 兼容位置 |
|---|---|---|
| stop reason/message | `RunExecutionState` | `Session.metadata` |
| Security Knowledge used | `RunExecutionState` | `Session.metadata` |
| finishing reminder sent | `RunExecutionState` | `Session.metadata` |
| Web Search budget | `RunExecutionState` | `Session.metadata` |

没有修改 Prompt、Tool Schema、权限、消息顺序、callback 顺序或 Streaming 协议。
应用级 Coding/Session 状态仍由原有持久边界管理，未被错误迁入 RunContext。

## 验证结果

- Phase 4 定向与相邻阶段测试：27 passed；
- Core Runtime：72 passed；
- Chat/Streaming：36 passed，22 skipped；
- Coding：20 passed；
- Tool：58 passed；
- Session/Memory：35 passed，7 skipped；
- Subagent：40 passed；
- 完整回归：409 passed，39 skipped；
- `pip check`：通过；
- `compileall`：通过；
- `git diff --check`：通过。

仓库没有配置独立 lint、format-check 或静态类型检查器，因此未伪造这些结果。

## 性能

详见 `benchmarks/results/runtime_phase4.json`。

| 场景 | Phase 3 median | Phase 4 median |
|---|---:|---:|
| Runtime facade no-tool | 0.704 ms | 0.718 ms |
| Chat no-tool | 0.696 ms | 0.700 ms |
| One tool | 1.308 ms | 1.358 ms |
| Three tools | 2.724 ms | 2.735 ms |

Runtime facade 变化约 +2%，属于本地微基准噪声范围；Context token 估算保持 Chat
730、Coding 2347，没有引入 MessageBus、序列化、持久化、Task Graph、完整 Trace、
Memory/RAG 或远程 Worker。

