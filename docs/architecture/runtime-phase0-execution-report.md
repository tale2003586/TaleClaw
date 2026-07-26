# TaleClaw Runtime Phase 0 执行报告

> 执行日期：2026-07-23
> 分支：`main`
> 基线提交：`bb4e845571428069a78a05526e71da8e41ddbcb7`

## 1. 执行结论

本轮完成了 Phase 0 的安全子集：

- 新增确定性 Scripted Model 和 Recording Tool；
- 新增 12 个 Runtime 行为与契约测试；
- 新增 Chat/Coding Context、Tool View 和关键 Tool Schema 快照；
- 记录 Chat、Coding、Subagent、Bootstrap、Tool 权限和 MessageBus 架构；
- 建立 Fake Model/Fake Tool 下的离线 Runtime 微基准；
- 未修改任何生产运行逻辑。

当前基线已能保护核心 Model-Tool Loop、基础 Chat 入口、Context section 顺序、
Tool View、关键 Tool Schema、初始 Cancellation 和 MessageBus 边界。

Phase 0 尚未完全覆盖 Coding 完整收尾、Memory/Trace 开关、Subagent/Coding
Cancellation 与性能、完整 Streaming 事件序列。因此，在 Phase 1 修改这些区域前，
仍需补充对应基线。

## 2. 仓库初始状态

| 项目 | 结果 |
|---|---|
| 当前分支 | `main` |
| 当前 HEAD | `bb4e845571428069a78a05526e71da8e41ddbcb7` |
| 与架构报告基线 | 一致 |
| 工作区 | 存在未跟踪文件 |
| 用户原有未跟踪文件 | `.DS_Store` |
| 已有分析文档 | `docs/agent-runtime-refactor-summary.md` |

本轮没有覆盖、删除、还原或提交用户原有文件。

## 3. 当前执行链

### Chat

```text
Gateway / AppRuntime
→ User MessageBus
→ AgentLoop
→ SessionManager
→ PluginManager.before_turn
→ ModeRouter
→ Pipeline
→ AgentRunner
→ ReasoningLoop
→ ContextBuilder
→ ModelProvider
→ ToolRegistry / ToolExecutor（有 Tool Call 时）
→ Session 保存
→ Outbound MessageBus
```

### Coding

```text
AgentLoop
→ ModeRouter
→ TaskSessionRunner
→ WorkspaceResolver
→ Coding Handoff
→ 隔离 Task Session
→ Working Memory 继承
→ Task-local Memory
→ Forked Pipeline
→ AgentRunner
→ ReasoningLoop
→ Working Memory 回写
→ Conclusion Extraction
→ Memory Promotion
→ Artifact / Workspace Diff
→ 最终回复
```

Coding 的 Workspace、Task Session、Handoff、Working Memory 同步、Artifact、
Workspace Diff、结论提取和 Memory Promotion 属于独立应用生命周期。

### Subagent

```text
task / parallel_tasks Tool
→ TaskSubagentRunner
→ 独立 Session
→ 复制父身份与 Workspace metadata
→ 继承 Working Memory snapshot
→ Agent Type Tool 白名单
→ 隔离 Pipeline / ContextBuilder
→ 共享 AgentRunner / ReasoningLoop
→ 结构化 SubagentResult
→ 父 Trace span
```

当前 Subagent 不继承父完整消息历史，也没有直接接收父 cancellation callback。

## 4. 新增测试资产

### 测试辅助设施

- `tests/fakes/scripted_model.py`
- `tests/fakes/fake_tools.py`

### 行为测试

- `tests/test_runtime_phase0_loop_baseline.py`
- `tests/test_runtime_phase0_contract_baseline.py`
- `tests/test_runtime_phase0_entrypoint_baseline.py`

### 快照

- `tests/snapshots/runtime_phase0_context.json`
- `tests/snapshots/runtime_phase0_tools.json`
- `tests/snapshots/runtime_phase0_lead_tools.json`

覆盖内容：

- Model 直接完成；
- 单次 Tool Call；
- Tool Call ID 与 Tool Result 关联；
- Tool Handler 异常；
- Step Limit；
- Model Timeout/Error；
- 首轮前 Cancellation；
- Streaming chunk 与最终文本一致性；
- Chat/Coding Context section 顺序；
- Context Token 估算；
- Chat/Coding Tool View；
- 真实 Lead Registry 数量；
- 关键 Tool Schema 哈希；
- `run_once` 与 `run_inbound` 的 MessageBus 边界。

## 5. Prompt、Context 与 Tool 基线

测试夹具中的 Context 结果：

| 场景 | 消息角色 | 估算 Token |
|---|---|---:|
| Chat | `system → user` | 340 |
| Coding | `system → user` | 487 |

完整项目指令下的基准场景：

| 场景 | 估算 Token |
|---|---:|
| Chat | 730 |
| Coding | 2347 |

当前真实 Lead Tool Registry：

| 指标 | 数量 |
|---|---:|
| 注册工具 | 50 |
| Chat 直接可见工具 | 11 |
| Coding 直接可见工具 | 29 |

关键 Tool Schema 已使用稳定 SHA-256 固定：

- `bash`
- `read_file`
- `write_file`
- `edit_file`
- `task`
- `parallel_tasks`
- `tool_search`

当前权限路径：

```text
enabled_modes / admin_only
→ ToolPolicy.visible_tools
→ schemas_for_turn
→ ToolPolicy.can_execute
→ ToolExecutor pre-hooks
→ ToolRegistry.execute
→ Handler
→ ToolExecutor post-hooks
```

## 6. 性能结果

环境：

- Python 3.14.6；
- macOS arm64；
- Fake Model；
- Fake Tool；
- 50 次迭代；
- 不访问网络、真实数据库或真实 Memory。

| 场景 | 迭代次数 | 中位数 | P95 |
|---|---:|---:|---:|
| Pipeline 测试装配 | 50 | 0.038 ms | 0.041 ms |
| 无工具 Chat | 50 | 0.686 ms | 0.763 ms |
| 单 Tool Call | 50 | 1.302 ms | 1.344 ms |
| 连续三 Tool Call | 50 | 2.734 ms | 2.849 ms |
| ToolExecutor wrapper | 50 | 0.0015 ms | 0.0016 ms |
| Chat Context | 50 | 0.181 ms | 0.196 ms |
| Coding Context | 50 | 0.215 ms | 0.232 ms |

结果文件：

```text
benchmarks/results/runtime_phase0.json
```

以上数据不包含完整 Bootstrap、真实数据库、RAG、Memory、Artifact、Workspace
Snapshot 或网络模型延迟。

## 7. 验证结果

| 命令/范围 | 结果 | 通过 | 失败 | 跳过 | 说明 |
|---|---|---:|---:|---:|---|
| 新增 Phase 0 测试 | 通过 | 12 | 0 | 0 | 全部离线 |
| 相关既有回归测试 | 部分通过 | 106 | 1 | 0 | 1 个 PostgreSQL 环境依赖失败 |
| 全量 `pytest -q` | 收集失败 | 0 | 2 errors | 0 | 测试环境缺少 `openai` |
| 新增文件 `py_compile` | 通过 | 6 | 0 | 0 | Python 语法检查 |
| `git diff --check` | 通过 | — | 0 | — | 无空白错误 |
| Lint | 未运行 | — | — | — | 仓库未配置 |
| Format Check | 未运行 | — | — | — | 仓库未配置 |
| Type Check | 未运行 | — | — | — | 仓库未配置 |
| Coverage | 未运行 | — | — | — | 仓库未配置 |

## 8. 发现的现有问题

| 问题 | 位置 | 是否阻塞 | 本轮处理 | 后续建议 |
|---|---|---:|---|---|
| Handler 异常最终仍标记 Tool success | `ToolRegistry` / `ToolExecutor` | 否 | 测试固化 | 单独设计错误契约迁移 |
| Subagent Trace 测试要求 PostgreSQL | `TraceStore/TraceIndexStore` | 部分 | 未修复 | 增加 Fake Index 或测试数据库 fixture |
| 全量测试环境无法由最小 dev 依赖运行 | `pyproject.toml` | 部分 | 记录 | 增加可复现 test extra/CI |
| Subagent 无直接父取消回调 | `TaskSubagentRunner.run` | 否 | 记录 | Phase 1 前补取消基线 |
| 完整 Bootstrap 不适合离线构造 | `runtime/bootstrap.py` | 否 | 使用受控等价基准 | 增加 Full Fake Bootstrap fixture |

## 9. 修改文件

| 文件/目录 | 操作 | 内容 | 影响生产逻辑 |
|---|---|---|---:|
| `tests/fakes/` | 新增 | 确定性 Model/Tool Fake | 否 |
| `tests/test_runtime_phase0_*` | 新增 | Runtime 行为与契约测试 | 否 |
| `tests/snapshots/` | 新增 | Context、Tool View、Schema 快照 | 否 |
| `benchmarks/runtime_phase0.py` | 新增 | 离线微基准 | 否 |
| `benchmarks/results/runtime_phase0.json` | 新增 | 基准结果 | 否 |
| `docs/architecture/runtime-phase0-baseline.md` | 新增 | 架构基线 | 否 |
| `docs/architecture/runtime-phase0-benchmark.md` | 新增 | 基准说明 | 否 |
| `docs/architecture/runtime-phase0-execution-report.md` | 新增 | 本次执行报告 | 否 |

生产模块修改数量：**0**。

## 10. Phase 1 前置条件

当前测试可保护：

- 核心 Model-Tool Loop；
- 基础 Chat 入口；
- 关键 Tool Schema 和 Tool View；
- Chat/Coding Context section 顺序；
- 基础 Token 估算；
- 初始取消和 Step Limit；
- User MessageBus 边界。

在修改以下区域前仍需补充：

1. Coding Task Session 完整生命周期；
2. Artifact、Workspace Diff、Conclusion、Promotion；
3. Long-term Memory 开关与失败行为；
4. 完整 Streaming 事件序列；
5. Tool 执行中 Cancellation；
6. Coding/Subagent Cancellation；
7. Trace/Memory 开关性能；
8. Coding Task Session 和 Subagent 性能；
9. Full Fake Bootstrap；
10. 可复现的全量测试依赖环境。

结论：

- 可以开始低风险的 AgentSpec 外围适配准备；
- 暂不建议开始 Coding、Memory、Streaming、Subagent 或 Bootstrap 的结构迁移；
- 本轮没有修改任何生产行为；
- 本轮是首次性能基线，没有历史对照，因此没有无法解释的性能变化。
