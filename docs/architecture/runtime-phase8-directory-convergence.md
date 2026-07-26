# TaleClaw Phase 8：目录结构收敛

> 日期：2026-07-23

## 目标

Phase 8 只调整物理包边界，不改变 Runtime、Prompt、Tool authorization、
Session 数据、Streaming 或应用行为。

## 移动映射

| 原路径 | 新路径 | 原因 |
|---|---|---|
| `agents/coding/` | `applications/coding/` | Coding 是产品应用生命周期，不是 AgentSpec |
| `coding_runtime/` | `applications/coding/orchestration/` | task、teammate、background 属于 Coding 编排 |
| `runtime/coding_context_state.py` | `applications/coding/context_state.py` | Coding 专属 Context |
| `runtime/coding_handoff.py` | `applications/coding/handoff.py` | Coding 专属 handoff |
| `bus/` | `runtime/messaging/` | MessageBus 是 Runtime 入口/协调子系统 |
| `sessions/` | `runtime/sessions/` | Session 是 Run/Application 的核心状态边界 |
| `runtime/background_memory.py` | `memory/background_lifecycle.py` | Background Memory 是 Memory 扩展 |

顶层新增 `applications/`，同时删除 `agents/coding`、`coding_runtime`、`bus` 和
`sessions` 四个分散边界，顶层生产包净减少三个。

## 目标结构

```text
applications/coding/
  orchestration/
agents/
  definitions.py
  subagent/
runtime/
  messaging/
  sessions/
  routing/
  trace/
memory/
```

Setuptools package discovery 和已跟踪的 egg-info 已同步更新。README、
PROJECT_STRUCTURE 与 CODEBASE_SUMMARY 反映当前真实路径。

## Gate

- 目录迁移定向测试：39 passed，3 skipped；
- Coding Context/Handoff：12 passed；
- 完整回归：416 passed，39 skipped；
- 安全/Tool/Workspace：47 passed，1 skipped；
- 离线 editable package 构建：通过；
- package import smoke：通过；
- `pip check`、`compileall`、`git diff --check`：通过；
- 仓库没有独立 lint、format-check 或 type-check 配置。

## 性能

| 场景 | Phase 7 | Phase 8 |
|---|---:|---:|
| Chat no-tool | 0.733 ms | 0.701 ms |
| Runtime facade | 0.710 ms | 0.697 ms |
| Subagent | 2.630 ms | 2.441 ms |

目录迁移没有引入运行时层级或性能退化。

