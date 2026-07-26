# TaleClaw Agent Runtime 工作总结

日期：2026-07-23  
分支：`refactor/agent-runtime-phase0-7`  
代码收口提交：`9c6409c`

## 一、今日目标

围绕 TaleClaw Agent Runtime 的复杂度和边界问题，完成从现状基线、统一执行入口、
应用隔离、物理目录收敛，到 Context 服务显式化的连续重构。

核心目标：

1. 取消 Chat/Coding 作为 Core Runtime 全局 Mode 的设计；
2. 让 Chat、Coding 和 Subagent 复用统一 `Runtime.run()`；
3. 将 Coding workspace、artifact、handoff 和 memory promotion 留在应用层；
4. 将 Working Memory、Retrieval、Skill、Instruction 等能力移出默认核心路径；
5. 在不改变 Prompt、权限和用户行为的前提下缩短核心调用链；
6. 建立可重复的行为、性能、安全和兼容性验证基线。

## 二、完成情况

今天完成 Phase 0–17，共 18 个阶段，形成 33 个本地提交。

### 1. 统一 Agent 执行模型

- 建立声明式 `AgentSpec`、`ModelPolicy`、`ToolSet`、`ContextPolicy`、
  `RunLimits` 和 `SpawnPolicy`；
- Chat、Coding 和 Subagent 统一通过 `Runtime.run()`；
- 引入显式 `RunContext`、Run state、取消和父子 Run 关系；
- Subagent 调整为独立 Child Run，使用隔离 Session 和受限 ToolRegistry。

### 2. 删除旧执行结构

- 删除生产路径中的 `ModeProfile`、旧 Mode 基类和 `ModeRouter` 依赖；
- 删除旧 `Pipeline` 执行路径；
- 将 Coding 保留为独立 `CodingApplication`；
- 将 Runtime 物理整理为 `context`、`execution` 和 `tooling` 子包。

### 3. 收敛执行核心

- 从 `ReasoningLoop` 提取模型调用、消息清理和 Tool batch 等无状态协作者；
- 将 Working Memory、Search、Batching 等能力放入显式可选策略；
- 建立最小 Kernel ports，约束 Context、Model、Tool、Lifecycle 和 Observability
  的依赖方向。

### 4. 收敛 Context 子系统

- 提取 `ContextRetrievalService`，统一 History Retrieval 和 Security RAG；
- 删除 10 个 Retrieval/Security 兼容构造参数；
- 提取 `PromptAssetsService`，负责 Instruction、Skill catalog、缓存和 fingerprint；
- 提取 `ContextMemoryService`，负责 Durable/Working Memory 渲染；
- 删除 5 个 Prompt Assets/Memory 兼容构造参数；
- Bootstrap、Coding、Subagent、Evaluation、测试和基准全部迁移为显式服务注入。

### 5. 文档与架构资产

- 持续维护 Phase 0–17 的设计说明、执行报告、状态和性能结果；
- 新增当前完整系统架构图，覆盖入口、应用、Runtime、Context、Tool、安全、
  Memory、RAG、Trace 和持久化边界；
- 在图中标记了 Runtime 读取 Coding team/background 通知的残余反向依赖。

## 三、量化指标

### 1. 变更规模

| 指标 | 数值 |
|---|---:|
| 完成阶段 | 18 个，Phase 0–17 |
| 本地提交 | 33 个 |
| 涉及文件 | 253 个 |
| 新增行 | 13,166 |
| 删除行 | 2,509 |
| Phase 15–17 涉及文件 | 34 个 |
| Phase 15–17 新增/删除 | 1,246 / 330 行 |
| 新系统架构图 | 273 行 Mermaid |

新增内容中包含大量测试、架构文档、快照和逐阶段 benchmark 结果，不能简单视为生产
复杂度增长。按文件分布统计，测试占本轮触及文件的 28.3%，架构文档占 17.3%。

### 2. 核心复杂度变化

| 模块 | 重构前 | 当前 | 变化 |
|---|---:|---:|---:|
| Context 核心 Builder | 1,748 行 | 1,264 行 | -484，下降 27.7% |
| ReasoningLoop | 2,041 行 | 1,736 行 | -305，下降 14.9% |
| Runtime Python 总行数 | 14,259 行 | 15,059 行 | +800，增加 5.6% |

Runtime 总行数增加主要来自显式服务、Kernel ports、测试支撑和物理分包；最大两个核心
类的职责和代码量均下降。当前成果更准确地说是“边界清晰化和复杂度分散”，尚未完成
整体代码量压缩。

### 3. 测试与质量

| 指标 | Phase 0 | Phase 17 |
|---|---:|---:|
| 完整测试通过 | 409 | 439 |
| Skip | 39 | 39 |
| 新增通过测试 | - | +30，增长 7.3% |
| 安全/Workspace/Tool 组合测试 | - | 61 passed，1 skipped |

其他质量门：

- `python -m compileall`：通过；
- `pip check`：无损坏依赖；
- `git diff --check`：通过；
- Prompt、Tool 权限和行为快照：无变化；
- PostgreSQL 集成测试仍需要 `DATABASE_URL`，保持条件跳过。

### 4. 性能指标

以下数据来自离线确定性 micro-benchmark，中位数单位为毫秒：

| 场景 | Phase 0 | Phase 17 | 变化 |
|---|---:|---:|---:|
| Chat，无 Tool | 0.692 | 0.734 | +6.1%，绝对增加 0.042ms |
| Chat Context 构建 | 0.201 | 0.189 | -5.8% |
| Coding Context 构建 | 0.279 | 0.224 | -19.5% |
| Subagent 创建并返回 | 2.485 | 1.676 | -32.6% |

Phase 17 新增的 `Runtime.run()` facade 测量为 0.716ms。Chat/Coding Prompt token
估算保持为 730/2347，没有因分层重构引入上下文膨胀。

总体判断：核心抽象和服务数量增加后，默认 Runtime 没有出现有意义的性能回退；
Coding Context 和 Subagent 路径有明显改善。

## 四、当前架构结果

当前主链路：

```text
Gateway / Web / CLI
→ AppRuntime
→ AgentLoop
→ AgentRouter
→ Chat Path / CodingApplication
→ Runtime.run
→ AgentRunner
→ ReasoningLoop
→ ContextBuilder / ModelProvider / ToolExecutor
```

Context 依赖已经显式化：

```text
ContextBuilder
├─ PromptAssetsService
├─ ContextMemoryService
├─ ContextRetrievalService
├─ Context Providers
└─ ContextBudgeter
```

CodingApplication 独立负责：

- workspace 解析与边界；
- 隔离 task session；
- context handoff；
- task memory；
- artifact 和 workspace diff；
- conclusion extraction；
- memory promotion；
- teammate 和 background task orchestration。

## 五、遗留问题

1. `ContextBuilder` 仍有 1,264 行，Coding context-state、history budgeting 和
   report composition 需要进一步审计；
2. `ReasoningLoop` 仍有 1,736 行，是当前最大的核心复杂度来源；
3. Runtime 仍会读取 Coding team/background 通知，存在应用层到核心的反向依赖；
4. `Session.metadata` 仍承担部分 Run、Workspace、Tool unlock 和 Coding handoff
   隐式状态；
5. `runtime/bootstrap.py` 的装配职责仍然较重；
6. 仓库尚未配置 lint、formatter check 和静态类型检查；
7. PostgreSQL 完整集成验证依赖外部 `DATABASE_URL`；
8. macOS `/var` 与 `/private/var` 的严格路径断言仍具有环境敏感性。

## 六、下一步建议

下一阶段不建议直接按文件大小继续拆类，先做职责与调用关系审计：

1. 划分 `ContextBuilder` 的 history、coding state 和 report 边界；
2. 梳理 `ReasoningLoop` 的主循环、termination、trace 和 optional policy 分支；
3. 清除 Runtime 对 Coding team/background bus 的反向依赖；
4. 将可迁移的 `Session.metadata` 状态收进 `RunContext` 或应用状态；
5. 增加 Ruff 和渐进式 Pyright/mypy，先覆盖 `runtime/context` 与
   `runtime/execution`；
6. 增加长会话、复杂 Coding、取消、权限拒绝和 Memory/RAG 开关的产品级基准。

## 七、结论

今天完成了 TaleClaw Agent Runtime 从 Mode/Pipeline 驱动结构向
“统一 Runtime + 应用编排 + 显式可选服务”的主体迁移。

重构过程中完整测试增加 30 个，核心 Prompt 和权限行为保持稳定；Context 核心代码量
下降 27.7%，ReasoningLoop 下降 14.9%，Coding Context 和 Subagent micro-benchmark
分别改善约 19.5% 和 32.6%。

当前架构边界已经基本成立，后续重点应从“迁移兼容路径”转向“降低核心实现复杂度和
清理隐式状态”。
