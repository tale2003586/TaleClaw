# TaleClaw Runtime Phase 1：统一 Agent 定义

> 实施日期：2026-07-23
> 基线提交：`bb4e845571428069a78a05526e71da8e41ddbcb7`

## 目标与边界

Phase 1 只统一 Agent 的静态定义：

1. 扩展 `AgentSpec`；
2. 将 Bot/Coding Profile 表达为完整 AgentSpec；
3. ModeRouter 同时输出 AgentSpec 与旧 ModeProfile；
4. Pipeline 优先执行显式 AgentSpec；
5. 保留 ModeProfile、ModeRouter、Pipeline 和现有执行链兼容。

`Runtime.run()` 属于 Phase 2，本阶段没有提前实现。ContextBuilder、Memory、
ToolRegistry、ReasoningLoop 和 Coding 生命周期均未重构。

## 新 Agent 定义

`runtime/agent_spec.py` 当前提供：

| 类型 | 职责 |
|---|---|
| `AgentSpec` | Agent 身份与全部静态策略的不可变集合 |
| `ModelPolicy` | 模型用途和可选模型名 |
| `ToolSet` | Agent 请求的 Tool Mode、allow/deny view |
| `ContextPolicy` | Context 策略名称及 Memory/History/Skills 开关 |
| `TerminationPolicy` | 停止策略声明 |
| `RunLimits` | Token、Reasoning Step、Tool Call 限制 |
| `SpawnPolicy` | 是否允许及允许哪些 Child Agent |

AgentSpec 不保存 Session 消息、Workspace、Gateway、数据库连接、Memory 内容、
Cancellation Event 或 Run Counter。

## 内置 Agent

### Bot

```text
BOT_AGENT_SPEC
├─ model purpose: chat
├─ tool mode: bot
├─ context policy: chat
├─ spawn: disabled
└─ compatibility profile: BOT_PROFILE
```

### Coding

```text
CODING_AGENT_SPEC
├─ model purpose: coding
├─ tool mode: coding
├─ context policy: coding
├─ spawn: explore / plan / code
└─ compatibility profile: CODING_PROFILE
```

Instructions 仍来自原 ModeProfile 的同一字符串，不改变 Prompt 内容或顺序。

## 兼容策略

- `AgentSpec.profile/model_purpose/max_tokens/max_reasoning_steps` 继续可用；
- 旧 AgentSpec 构造调用自动生成结构化 Policy；
- `AgentSpec.from_profile()` 是 ModeProfile 迁移入口；
- `AgentSpec.with_limits()` 返回新对象，不修改原定义；
- `RouteResult.profile` 保留，新增 `RouteResult.agent_spec`；
- `last_route` 保留原 key，新增 `agent`；
- Pipeline 未收到 AgentSpec 时仍从旧 Profile 构建兼容定义；
- 测试 Fake Router 没有 `agent_spec` 时继续工作。

## 实际调用变化

```text
ModeRouter
→ RouteResult(profile=旧兼容值, agent_spec=新定义)
→ AgentLoop
→ Pipeline.run(profile, agent_spec)
→ AgentRunner.run_turn(spec)
→ 原 ReasoningLoop
```

Coding 路径将 AgentSpec 透传经过 TaskSessionRunner，再交给 forked Pipeline。

## 验证结果

核心回归：

```text
Phase 0 + Phase 1 + 路由/Coding/Model/Reflection：60 passed, 1 skipped
Scripted Coding Benchmark：6 passed
Trace/Context/Tool 扩展回归：75 passed, 2 skipped
```

性能对比保存于：

```text
benchmarks/results/runtime_phase1.json
```

关键中位数：

| 场景 | Phase 0 | Phase 1 | 判断 |
|---|---:|---:|---|
| Pipeline construction | 0.0370 ms | 0.0370 ms | 无明显变化 |
| Chat no-tool | 0.6922 ms | 0.6798 ms | 无退化 |
| One tool | 1.3463 ms | 1.3136 ms | 无退化 |
| Three tools | 2.8351 ms | 2.7192 ms | 无退化 |
| Coding context | 0.2786 ms | 0.2109 ms | 无退化 |
| Subagent | 2.4851 ms | 2.4488 ms | 无退化 |

微秒级差异属于同机运行波动，本阶段没有引入额外 I/O、序列化、数据库或网络。

## Phase 1 完成判断

Phase 1 的三个目标均已实现：

- AgentSpec 已扩展；
- Bot/Coding 已有正式 AgentSpec；
- ModeRouter 兼容层仍在并输出新定义。

下一阶段可以新增统一 `Runtime.run()` facade，但必须继续代理现有 Pipeline 和
ReasoningLoop，不能在同一切片内重写执行循环。
