# TaleClaw Runtime Phase 3：执行循环职责收敛

> 实施日期：2026-07-23

## 目标

保持模型—工具循环行为不变，将应用/Agent 策略从 `ReasoningLoop` 的具体实现中
提取出来，并由 `AgentRunner` 装配。

## 新职责边界

```text
Runtime
→ Pipeline（兼容 Context/Memory adapter）
→ AgentRunner（一次 Run 的策略装配和生命周期入口）
→ ReasoningLoop（Model → Tool → Model 机制）
```

Pipeline 暂时保留，因为 Phase 5 之前 ContextBuilder 尚未 Policy 化，Phase 7
才删除兼容层。本阶段不为减少类数量而提前破坏 Context 与 Memory 行为。

## 提取的策略

`runtime/loop_policies.py`：

| Policy | 从 ReasoningLoop 移出的职责 |
|---|---|
| `WebSearchBudgetPolicy` | 每 turn 搜索计数、拒绝和结果提示 |
| `FinishingReminderPolicy` | Coding/长链路收尾提醒时机、消息和事件 payload |
| `WorkingMemoryPolicy` | Coding checkpoint、stop、complete |
| `ToolBatchPolicy` | task 并行化与 read_file batching 判定 |

AgentRunner 构造并持有 Web Search、Working Memory 和 Tool Batch policy；每次 Run
按本次 limits 构造 Finishing policy，再注入 ReasoningLoop。

ReasoningLoop 仍负责：

- Reasoning step；
- Model 调用；
- Tool 调用；
- Cancellation/limits；
-最小 Trace；
-错误与最终输出边界。

它不再直接实现上述四类业务策略。

## 兼容保证

- 原 metadata key 保留；
- 原 finishing reminder 文本逐字保留；
- 原 Web Search denial/notice 文本保留；
- 原 Working Memory 调用顺序与 checkpoint callback 保留；
- 原 parallel_tasks/read_files 触发条件保留；
- 原 ReasoningLoop 私有兼容方法继续委托 policy，避免现有测试断裂。

## 验证

```text
Phase 3 policy/loop/working-memory/reflection：47 passed
Phase 0～2 + Coding Benchmark：22 passed
```

最终完整门禁见执行回复文档。

性能：`benchmarks/results/runtime_phase3.json`

| 场景 | Median |
|---|---:|
| Pipeline construction | 0.037 ms |
| Chat no-tool | 0.696 ms |
| Runtime facade no-tool | 0.704 ms |
| One tool | 1.308 ms |
| Three tools | 2.724 ms |
| Subagent | 2.474 ms |

没有发现明显退化。

## Phase 3 完成判断

- AgentRunner 成为策略装配点；
- ReasoningLoop 收敛为机制循环；
- Working Memory、Web Search、batching、finishing 的实现已移出核心循环；
- Pipeline 仅因 Context/Memory 兼容职责保留，不再作为未来统一入口。

下一阶段是 Phase 4：建立统一 RunContext，并将 Run 临时状态从
`Session.metadata` 迁出；迁移期必须双写。
