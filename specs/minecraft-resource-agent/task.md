# Minecraft 资源任务 Agent Tasks

## 1. 文档目的

本文档把 Minecraft Agent 拆成四个可独立验收的实施阶段。核心架构结论是：

> `AgentLoop` 是 TaleClaw Kernel 之上的一种通用聊天执行策略，不是 Kernel 本身。  
> `MinecraftWorker` 是 MinecraftApplication 的领域执行循环。它直接复用 Kernel 的模型、Memory、Context、Session、Trace、Cancellation、MessageBus、配置和依赖注入能力，但不依赖、不嵌套、也不伪造消息调用 `AgentLoop`。

本文件是后续 Codex 自动化实施说明。本阶段只修改设计文档，不实现业务代码。

## 2. 真实代码核对结果

| 能力 | 当前真实模块 | 当前职责与设计结论 |
|------|--------------|--------------------|
| 通用聊天循环 | `runtime/agent_loop.py::AgentLoop` | 接收 `MessageBus` 输入、路由、调用 `Runtime` 或 `CodingApplication`、回复用户；Minecraft 长任务创建后不再由它逐动作驱动 |
| 应用外壳 | `runtime/app_runtime.py::AppRuntime` | 包装 `MessageBus` 和 `AgentLoop`；当前取消直接转发给 AgentLoop |
| 依赖装配 | `runtime/bootstrap.py::build_runtime` | 创建模型池、Session、Trace、Context、Memory、Tool、Plugin、Runtime、CodingApplication、AgentLoop；需要抽取可复用依赖容器 |
| 一次性模型任务 | `models/model_task_runner.py::ModelTaskRunner` | 可直接调用模型且不需要工具或 Session 生命周期；Minecraft Planner/LLMCritic 直接依赖它，不经过 AgentLoop |
| 模型池/路由 | `models/model_pool.py::ModelPool`、`RoutedModelProvider` | 提供按 purpose 的模型选择与故障转移；Minecraft 不创建第二个模型池 |
| 通用推理 Runtime | `runtime/runtime.py::Runtime` | Chat/Coding 的通用模型工具循环；不作为 MinecraftWorker 的持续执行循环 |
| Agent 定义 | `runtime/agent_spec.py::AgentSpec` | 可声明模型 purpose、tool mode、上下文和预算；Phase C 用于聊天入口 |
| 工具注册 | `tools/tool_registry.py::ToolRegistry` | 工具可见性、session 授权和调用入口；只注册面向外部的 Minecraft task 工具 |
| 工具执行 | `tools/executor.py::ToolExecutor` | hooks、风险检查和 trace；不包装每一次 Bridge 内部轮询 |
| 插件管理 | `plugins/plugin_manager.py::PluginManager` | 注册 Plugin 工具与 hooks；Phase C 注册 `MinecraftPlugin` |
| Memory | `memory/store.py::MemoryStore`、`memory/scoped_store.py::ScopedMemoryStore`、向量索引模块 | Minecraft 增加预算化 adapter，按规划/全局重规划/LLMCritic/checkpoint 读取相关记忆，不逐轮读取全部通用记忆 |
| Context | `runtime/context/builder.py::ContextBuilder`、`runtime/context/budget.py::ContextBudgeter`、Context services | 普通 `ContextBuilder` 偏聊天历史；Minecraft 创建 Planner/Critic builder，复用预算、裁剪、Memory 和 prompt assets，不注入普通聊天历史 |
| Session | `runtime/sessions/session.py::SessionManager`、`runtime/sessions/session_store.py::SessionStore` | 保存 UserSession；Minecraft 世界状态放在独立 Task/Run，不塞入聊天 Session metadata |
| Trace | `runtime/trace/trace_store.py::TraceStore`、`runtime/trace/run_state.py::RunState` | 复用 run/event 记录与订阅，增加 Minecraft 事件名和 adapter |
| Cancellation | `runtime/agent_loop.py` 内 `_cancel_events` | 当前没有独立 Registry；合理抽取为 `runtime/cancellation.py`，供 AgentLoop 和 MinecraftWorker 共享 |
| 用户消息总线 | `runtime/messaging/user_bus.py::MessageBus` | 只提供 inbound/outbound 用户消息，不虚构为通用领域 EventBus；任务内部事件走 Store/Trace，用户进度通过 adapter 发 OutboundMessage |
| Coding Application | `applications/coding/runner.py::CodingApplication` | 已存在领域 Application 生命周期范例；MinecraftApplication 采用独立 Worker，但不复制 Coding 的任务管线 |
| 路由 | `runtime/routing/intent.py`、`execution_plan.py`、`agent_router.py` | 当前只有 bot/coding/hybrid 语义；显式 Minecraft 入口先做，自动分类延后且可选 |
| 数据库 | `runtime/db.py` | 当前只接受 PostgreSQL DSN；Phase A/B 用 Store Protocol + InMemory 实现，Phase D 才接 PostgreSQL |

文档不假设当前存在 `CancellationRegistry`、通用领域 EventBus、Minecraft tool mode 或结构化 `ModelTaskRunner` API；它们分别被列为明确的抽取、适配或新增任务。

## 3. 最终架构

```text
                              TaleClaw Kernel
  ModelPool / ModelTaskRunner / PluginManager / ToolRegistry / ToolExecutor
  Memory / ContextBudgeter / SessionManager / TraceStore / CancellationRegistry
                 MessageBus / config / bootstrap / logging
                                  │
              ┌───────────────────┼────────────────────┐
              │                   │                    │
          AgentLoop          API / CLI             Benchmark
              │                   │                    │
              └─────────────┬─────┴────────────────────┘
                            ▼
                  MinecraftTaskService
                            │
                            ▼
                    MinecraftWorker
                    ├── SafetyController
                    ├── LocalEvaluator
                    ├── ReasoningGate
                    ├── Planner
                    ├── LLMCritic
                    ├── BeliefWorldState
                    ├── DomainCatalog
                    ├── MemoryAdapter
                    └── BridgeClient
                            │
                            ▼
                   Node.js Mineflayer Bridge
```

### 3.1 三层边界

**TaleClaw Kernel**

- 负责模型池与模型任务执行、Plugin/Tool、Memory、Context 基础服务、UserSession、Trace、Cancellation、MessageBus、配置、日志和 bootstrap。
- 不知道 Minecraft 方块、背包、工具等级、世界状态或采矿策略。
- `AgentLoop` 不属于 Kernel 核心能力；它消费 Kernel 能力实现聊天执行策略。

**Application Runtime**

```text
TaleClaw Kernel
├── ChatApplication
│   └── AgentLoop
├── CodingApplication
│   └── CodingApplication / Runtime
└── MinecraftApplication
    └── MinecraftWorker
```

**MinecraftWorker**

- 循环：读取任务 → 有限观察 → 更新信念世界 → SafetyController → LocalEvaluator → ReasoningGate → 必要时 Planner/LLMCritic → 执行高层动作 → 验证真实变化 → 保存事件/checkpoint → 进入终态或下一轮。
- 可以直接调用 `ModelTaskRunner`，但模型不是循环本身。
- 不重新实现 ModelPool、通用模型路由、PluginManager、ToolRegistry、Memory 存储、SessionStore、TraceStore、CancellationRegistry 或 MessageBus。

禁止依赖：

```text
MinecraftWorker → AgentLoop → ModelTaskRunner
```

允许依赖：

```text
MinecraftWorker → ModelTaskRunner
MinecraftWorker → Memory/Context/Trace/Cancellation
MinecraftWorker → BridgeClient
MinecraftTaskService → MinecraftWorker
```

### 3.2 三种入口

**入口 A：聊天**

```text
用户消息
→ AgentLoop
→ MinecraftPlugin.minecraft_start_task
→ MinecraftTaskService
→ MinecraftWorker
```

AgentLoop 只识别/接收显式指令、创建/查询/取消任务并返回状态。任务创建后不参与逐动作执行和持续认知。

**入口 B：API、CLI、Benchmark**

```text
API / CLI / Benchmark
→ MinecraftTaskService
→ MinecraftWorker
```

该入口完全绕过 AgentLoop，用于自动化测试、批量实验和开发调试。

**入口 C：显式 Minecraft 模式**

MVP 优先支持 `/minecraft 收集 4 个原木` 或调用方显式传入 `application_mode=minecraft`。自动自然语言意图分类是 Phase C 的可选增强，不阻塞最小闭环。

所有入口必须解析成相同的 `CreateMinecraftTaskRequest` 并调用同一个 `MinecraftTaskService`。

## 4. 核心边界与接口

### 4.1 Store

```python
class MinecraftTaskStore(Protocol):
    def create(self, task: MinecraftTask, *, idempotency_key: str) -> MinecraftTask: ...
    def get(self, task_id: str) -> MinecraftTask | None: ...
    def update(self, task: MinecraftTask, *, expected_version: int) -> MinecraftTask: ...
    def append_event(self, event: MinecraftTaskEvent) -> None: ...
    def save_checkpoint(self, checkpoint: MinecraftCheckpoint) -> None: ...
    def list_recoverable(self) -> list[MinecraftTask]: ...
```

- Phase A/B：`InMemoryMinecraftTaskStore`。
- Phase D：`PostgresMinecraftTaskStore`。
- Service、Worker、ReasoningGate 只依赖 Protocol，不依赖 PostgreSQL。

### 4.2 世界状态

**BotObservation** 是 Bridge 在当前时刻返回的有限真实事实：位置、维度、生命、饥饿、氧气、背包、装备、附近资源/掉落/危险和当前动作。

**BeliefWorldState** 是多次观察合并出的任务信念：已知资源位置、探索区域、失败路径、安全位置、工作台位置、历史失败等。可能过时的事实必须带 `observed_at`、`expires_at`、`confidence`、`source`。

**DomainCatalog** 是静态 Minecraft 事实：别名、配方、掉落物、工具等级、依赖和版本方块映射。

```text
BeliefWorldState：Agent 认为当前世界是什么样
DomainCatalog：Minecraft 的规则是什么
BotObservation：当前时刻 Bridge 实际看到了什么
```

静态规则不得复制进 `BeliefWorldState`。

### 4.3 ReasoningGate

`applications/minecraft/reasoning_gate.py` 是所有模型调用的唯一批准点。输入包括任务、计划版本、信念世界、关键事件、动作结果、本地评估、恢复次数、无进展时间、动作/模型预算、上次调用时间和已处理事件 ID。

输出 `CognitiveDecision`：

- `CONTINUE_PLAN`
- `EXECUTE_NEXT_STEP`
- `LOCAL_RECOVERY`
- `CALL_PLANNER`
- `CALL_LLM_CRITIC`
- `SAFETY_INTERRUPT`
- `COMPLETE_TASK`
- `BLOCK_TASK`
- `FAIL_TASK`

每个决定包含 `reason_code`、`event_id`、`plan_version`、`consumes_model_budget`、`cancel_current_action` 和 `suggested_next_step_type`。

ReasoningGate 确定性实现事件去重、聚合、防抖、普通调用冷却、同类失败计数、模型预算、安全事件绕过模型、重复策略排除和计划版本校验。Planner 首次调用、PlanRevision 和 LLMCritic 都必须持有 Gate 生成的批准决定。

### 4.4 LocalEvaluator 与 LLMCritic

`LocalEvaluator` 不调用模型。输入 PlanStep、动作前后观察、ActionResult、目标、预算和重试状态，输出：

- `continue`
- `step_succeeded`
- `local_recovery`
- `escalate`
- `task_completed`
- `blocked`
- `failed`

它判断动作是否成功、预期状态是否发生、净新增是否变化、是否接近目标、是否有本地恢复、是否超过重试和是否无进展。

`LLMCritic` 只在 ReasoningGate 批准后调用 `ModelTaskRunner`，用于复杂偏差、多个策略失败、未知环境、长期低效或多个高层方案选择。禁止每动作调用 Critic。

### 4.5 模型、Memory 与 Context

- Planner、PlanRevision、LLMCritic 直接调用 `ModelTaskRunner`，不经过 AgentLoop。
- 为 planner/revision/critic 使用不同 `AgentSpec.model_purpose` 或配置。
- 结构化输出、超时、usage/token、Trace、调用预算和失败降级由 Kernel 增强接口与 Minecraft adapter 共同承接。
- `MinecraftMemoryAdapter` 复用 `ScopedMemoryStore`/底层索引，保存世界位置、历史任务、失败策略、成功计划和技能经验。
- 只在初始规划、全局重规划、LLMCritic 和重要 checkpoint 按预算检索；普通动作轮次不读取全部通用记忆。
- `MinecraftPlannerContextBuilder` 和 `MinecraftCriticContextBuilder` 复用 `ContextBudgeter`、Memory 注入、prompt assets 和内容裁剪；不直接使用普通聊天历史。

### 4.6 外部 Tool 与内部动作

通过 `MinecraftPlugin` 注册的 TaleClaw 外部工具：

- `minecraft_start_task`
- `minecraft_get_status`
- `minecraft_cancel_task`
- `minecraft_get_bot_status`

MinecraftWorker 内部动作：

- `observe`
- `find_blocks`
- `collect_blocks`
- `craft`
- `equip`
- `eat`
- `return_safe`
- `branch_mine`
- `cancel_action`

内部动作由 Worker 通过 BridgeClient 调用，不强制包装成 ToolRegistry 的 LLM tool call。它们仍使用统一 DTO/schema、安全策略、错误分类和 Trace。

### 4.7 动作监控

```python
class BridgeClient(Protocol):
    async def submit_action(self, action: BridgeAction) -> ActionHandle: ...
    async def watch_action(
        self,
        action_id: str,
        cancellation_token: CancellationToken,
    ) -> AsyncIterator[ActionEvent]: ...
```

Phase A 的 HTTP client 用有界 polling 实现 `watch_action`。Worker 只依赖 AsyncIterator 接口；Phase D 可替换为 SSE、WebSocket 或长轮询，但本次不要求全部实现。

### 4.8 Session、Trace、Cancellation 与事件

```text
UserSession
└── MinecraftTask
    └── MinecraftRun
```

MinecraftTask 至少保存 task/user/session/bot ID、goal、state、plan version、world checkpoint、cognitive/action budget、trace ID 和 cancellation scope。长期世界状态不写入普通聊天 Session。

Trace 事件：

`TASK_CREATED`、`INITIAL_OBSERVATION`、`PLAN_REQUESTED`、`PLAN_GENERATED`、`PLAN_VALIDATED`、`PLAN_REJECTED`、`REASONING_GATE_DECISION`、`ACTION_STARTED`、`ACTION_PROGRESS`、`ACTION_COMPLETED`、`ACTION_FAILED`、`LOCAL_RECOVERY`、`LLM_CRITIC_REQUESTED`、`PLAN_REVISED`、`SAFETY_INTERRUPTED`、`CHECKPOINT_SAVED`、`TASK_COMPLETED`、`TASK_BLOCKED`、`TASK_FAILED`、`TASK_CANCELLED`。

每次模型调用 trace 必须说明调用原因、触发 event、plan version、模型预算消耗、上下文规模、返回结果和 Validator 结果。

取消传播：

```text
用户/API 取消
→ MinecraftTaskService
→ 持久化取消状态
→ CancellationRegistry/CancellationToken
→ MinecraftWorker
→ Bridge 当前动作取消
→ 停止后续动作
```

内部领域事件写 Store/Trace；面向用户的进度由 `MinecraftProgressPublisher` 转换成现有 `MessageBus.publish_outbound()`。不把当前 MessageBus 虚构成通用领域 EventBus。

### 4.9 安全能力模式

定义可扩展 `SafetyCapabilityProfile`：

- `restricted_resource_mode`（Phase A 默认且唯一实现）
- `survival_mode`（后续）
- `builder_mode`（后续）
- `combat_mode`（后续）
- `cooperative_mode`（后续）

Phase A 的 restricted mode 禁止主动攻击、容器访问、爆炸物、岩浆操作、非白名单方块破坏、聊天命令、原始协议和任意代码。安全策略是可替换 profile，不是整个 Minecraft Agent 永久能力上限。

## 5. 确定性 LLM 调用决策表

| 场景 | 调用模型 | ReasoningGate 决策/处理 |
|------|---------:|--------------------------|
| 新任务且没有计划 | 是 | `CALL_PLANNER` |
| 当前动作正常推进 | 否 | `CONTINUE_PLAN` |
| 当前计划步骤成功 | 通常否 | `EXECUTE_NEXT_STEP` |
| 滚动计划仍有合法步骤 | 否 | `EXECUTE_NEXT_STEP` |
| 滚动计划耗尽且目标未完成 | 是 | `CALL_PLANNER` 扩展计划 |
| 一次普通寻路失败 | 否 | `LOCAL_RECOVERY` |
| 同类寻路失败达到上限 | 是 | `CALL_PLANNER` 或 `CALL_LLM_CRITIC` |
| 工具损坏但有备用工具 | 否 | `LOCAL_RECOVERY`，本地装备 |
| 工具损坏且前置条件失效 | 是 | `CALL_PLANNER` |
| 背包满且有本地处理策略 | 否 | `LOCAL_RECOVERY` |
| 生命、饥饿、氧气或熔岩危险 | 否 | `SAFETY_INTERRUPT` |
| 安全恢复后原计划仍有效 | 否 | `EXECUTE_NEXT_STEP` |
| 安全恢复后原计划失效 | 是 | `CALL_PLANNER` |
| 发现普通新资源 | 否 | 更新 BeliefWorldState |
| 新发现显著改变长期策略 | 可选 | Gate 根据收益、冷却和预算决定 |
| 长时间无进展 | 是 | `CALL_LLM_CRITIC` 或 `CALL_PLANNER` |
| 模型预算耗尽 | 否 | fallback、`BLOCK_TASK` 或 `FAIL_TASK` |
| 用户取消 | 否 | 立即取消 |
| 真实背包验证目标完成 | 否 | `COMPLETE_TASK` |

里程碑完成本身不触发模型。仅当滚动计划耗尽、存在多个高层分支、原计划前提失效，或世界发生规则无法处理的显著变化时，里程碑后才允许 Gate 批准模型调用。

## 6. Phase A：最小垂直闭环

**阶段目标：** `/minecraft 收集 4 个原木` 或直接调用 Service，Bot 真实完成任务。Phase A 不依赖 PostgreSQL、LLM、自动意图分类、LLMCritic、完整恢复、Docker Compose、多 Agent 或完整五资源科技树。

### A01：领域 DTO 与边界 Protocol

**目标：** 建立不依赖 AgentLoop、PostgreSQL 和 Mineflayer 对象的领域合同。  
**文件：** 新建 `applications/minecraft/__init__.py`、`models.py`、`ports.py`、`tests/minecraft/test_models.py`  
**前置依赖：** 无  
**实施步骤：** 定义 Goal、Task、Run、Budget、BotObservation、Action、ActionEvent、Report、Store/Bridge/Trace/Progress Protocol；严格校验数量、状态、列表和未知字段。  
**非目标：** 不实现存储、网络、LLM 或完整资源科技树。  
**验证命令：** `pytest -q tests/minecraft/test_models.py`  
**验收结果：** DTO 可稳定序列化；Protocol 不引用 AgentLoop、psycopg 或 Mineflayer。  
**失败诊断：** 检查循环 import、Pydantic extra policy、异步接口签名。

### A02：原木 Catalog 与目标 Parser

**目标：** 支持单一“收集 N 个原木”目标。  
**文件：** 新建 `applications/minecraft/catalog.py`、`parser.py`、`tests/minecraft/test_parser.py`  
**前置依赖：** A01  
**实施步骤：** 区分 DomainCatalog 与世界状态；加入原木别名、方块/掉落物和白名单；解析显式命令并拒绝零/负数、多目标和非资源任务。  
**非目标：** 不实现铁、钻石科技树或自动路由。  
**验证命令：** `pytest -q tests/minecraft/test_parser.py`  
**验收结果：** “收集 4 个原木”规范化成功，非法目标返回稳定错误码。  
**失败诊断：** 检查别名冲突、数量正则和多目标检测。

### A03：净新增、基础状态机与报告

**目标：** 以真实背包差值完成任务并生成结构化终态。  
**文件：** 新建 `applications/minecraft/state_machine.py`、`reporting.py`、`tests/minecraft/test_progress.py`  
**前置依赖：** A01  
**实施步骤：** 实现 `max(0, current-baseline)`、合法状态转换、成功/失败/阻塞/取消报告；事件累计不作为完成真值。  
**非目标：** 不做智能规划或持久恢复。  
**验证命令：** `pytest -q tests/minecraft/test_progress.py`  
**验收结果：** 基线 5、目标 4 时当前 8 未完成、9 完成；丢失物品会回退进度。  
**失败诊断：** 检查 baseline 捕获时机、物品规范 ID 和终态幂等。

### A04：抽取 Kernel CancellationRegistry

**目标：** 提供 Worker 可直接使用的共享取消基础设施。  
**文件：** 新建 `runtime/cancellation.py`、`tests/minecraft/test_cancellation.py`  
**前置依赖：** 无  
**实施步骤：** 定义线程安全 CancellationToken/Registry，支持 scope 创建、请求、查询、释放和重新注册；暂不改 AgentLoop。  
**非目标：** 不在本任务接入聊天回合取消或数据库取消状态。  
**验证命令：** `pytest -q tests/minecraft/test_cancellation.py -k registry`  
**验收结果：** 同 scope 共享 token，释放后新 token 不继承旧取消。  
**失败诊断：** 检查锁粒度、scope 正规化和释放竞态。

### A05：InMemoryTaskStore

**目标：** 让 Phase A/B 无 PostgreSQL 运行。  
**文件：** 新建 `applications/minecraft/stores/__init__.py`、`stores/memory.py`、`tests/minecraft/test_memory_store.py`  
**前置依赖：** A01  
**实施步骤：** 实现 Store Protocol、幂等创建、版本条件更新、事件/checkpoint 内存记录和单 bot 活动任务约束。  
**非目标：** 不实现进程重启恢复或 SQL。  
**验证命令：** `pytest -q tests/minecraft/test_memory_store.py`  
**验收结果：** 重复 idempotency key 返回同任务，并发版本冲突可诊断。  
**失败诊断：** 检查深拷贝隔离、锁和版本递增。

### A06：Fake Bridge 与确定性模拟世界

**目标：** 在无网络、无真实服务器条件下测试 Worker。  
**文件：** 新建 `tests/minecraft/fakes.py`、`tests/minecraft/test_fake_bridge.py`  
**前置依赖：** A01, A02  
**实施步骤：** 模拟观察、find/collect、背包、掉落、幂等动作、取消和虚拟时间；相同 seed 产生相同结果。  
**非目标：** 不模拟完整 Minecraft 物理或全部动作。  
**验证命令：** `pytest -q tests/minecraft/test_fake_bridge.py`  
**验收结果：** 无外部网络即可确定性执行原木采集。  
**失败诊断：** 检查虚拟时钟、动作 idempotency key 和 fixture 状态泄漏。

### A07：Bridge Node 包、配置与 schema

**目标：** 建立可启动的最小 Mineflayer Bridge。  
**文件：** 新建 `minecraft-bridge/package.json`、lockfile、`src/config.js`、`src/schemas.js`、对应 Node tests  
**前置依赖：** 无；可与 A01–A06 并行  
**实施步骤：** 固定 Node 18+ 与 Mineflayer 生态依赖；默认 loopback、offline auth、自动版本；定义 connect/observe/find/collect/cancel schema 和请求大小限制。  
**非目标：** 不实现远程绑定、SSE、craft/equip 或 branch mining。  
**验证命令：** `cd minecraft-bridge && npm ci && npm test -- config.test.js schemas.test.js`  
**验收结果：** 非法端口、空 token、原始协议和任意代码字段被拒绝。  
**失败诊断：** 检查 lockfile、Node engine、schema strict mode 和环境变量。

### A08：Bridge 连接与有限观察

**目标：** 以离线账号连接并返回 BotObservation。  
**文件：** 新建 `minecraft-bridge/src/bot-adapter.js`、`test/bot-adapter.test.js`  
**前置依赖：** A07  
**实施步骤：** 注入 Mineflayer factory；处理 spawn/disconnect/kicked；未指定版本时自动协商；裁剪位置、生存值、背包、附近原木和危险。  
**非目标：** 不把 Mineflayer 实例或逐 tick 事件暴露给 Python。  
**验证命令：** `cd minecraft-bridge && npm test -- bot-adapter.test.js`  
**验收结果：** fake bot 覆盖连接、自动版本和有限观察，超量数据被裁剪。  
**失败诊断：** 检查事件解绑、版本字段和观察上限。

### A09：动作幂等、取消与 restricted_resource_mode

**目标：** 为原木任务提供最小安全动作生命周期。  
**文件：** 新建 `minecraft-bridge/src/action-store.js`、`src/safety.js`、对应 tests  
**前置依赖：** A07  
**实施步骤：** 实现动作状态、同 key 去重、每 bot 单活动动作、AbortController；定义可扩展 SafetyCapabilityProfile 并只实现 restricted mode。  
**非目标：** 不实现 builder/combat 等模式，也不把 restricted mode 定义成永久上限。  
**验证命令：** `cd minecraft-bridge && npm test -- action-store.test.js safety.test.js`  
**验收结果：** 取消幂等；攻击、容器、命令、爆炸物、岩浆、非白名单破坏、代码和原始协议被拒绝。  
**失败诊断：** 检查终态转换、重复 handler 启动和 profile 白名单。

### A10：Bridge find_blocks 与 collect_blocks

**目标：** 在真实 Bot Adapter 上采集原木。  
**文件：** 新建 `minecraft-bridge/src/actions/collect.js`、`src/server.js`、`src/index.js`、对应 tests  
**前置依赖：** A08, A09  
**实施步骤：** 使用 minecraft-data、pathfinder、collectblock；实现 health/connect/state/action/watch-poll/cancel HTTP 路由；动作前后执行安全校验。  
**非目标：** 不实现制作、装备、分支采矿或真实服务器自动 CI。  
**验证命令：** `cd minecraft-bridge && npm test -- collect-actions.test.js server.test.js`  
**验收结果：** fake Mineflayer 可发现并收集原木；重复请求不重复挖掘；取消立即终止。  
**失败诊断：** 检查方块版本映射、插件加载顺序、AbortSignal 和 HTTP 错误分类。

### A11：Python BridgeClient 与可替换 watch_action

**目标：** Worker 不依赖具体 polling 传输。  
**文件：** 新建 `applications/minecraft/bridge_client.py`、`tests/minecraft/test_bridge_client.py`  
**前置依赖：** A01, A04；可与 A07–A10 并行开发  
**实施步骤：** 用 httpx 实现 Bridge Protocol；`watch_action()` 返回 AsyncIterator，底层采用有界 polling；传播 CancellationToken、超时、幂等和错误分类。  
**非目标：** 不实现 SSE/WebSocket，也不允许聊天输入覆盖 Bridge URL。  
**验证命令：** `pytest -q tests/minecraft/test_bridge_client.py`  
**验收结果：** Fake HTTP 覆盖成功、失败、超时、重复和取消；替换 fake watcher 不影响 Worker 接口。  
**失败诊断：** 检查 async generator 关闭、超时层级和 token 脱敏。

### A12：最小 TaskService、Worker 与原木 E2E

**目标：** 交付 Phase A 独立闭环。  
**文件：** 新建 `applications/minecraft/service.py`、`worker.py`、`fixed_planner.py`、`tests/minecraft/test_wood_e2e.py`、`scripts/minecraft_smoke.py`  
**前置依赖：** A02–A06, A11；真实 smoke 另依赖 A10  
**实施步骤：** Service 捕获基线并启动 Worker；Worker 用固定原木计划执行 observe/find/collect、验证动作后观察、取消和终态报告；smoke 默认只检查配置，显式 `--connect` 才连接。  
**非目标：** 不调用 LLM、不依赖 AgentLoop/PostgreSQL、不承诺钻石或重启恢复。  
**验证命令：** `pytest -q tests/minecraft/test_wood_e2e.py && python scripts/minecraft_smoke.py --check-only`  
**验收结果：** “收集 4 个原木”从创建到净新增 4 后 succeeded；测试无真实服务器依赖。  
**失败诊断：** 检查基线时机、Worker 重入锁、动作后观察和 fake 世界 seed。

## 7. Phase B：智能规划与认知门控

**阶段目标：** “制作一把石镐”由动态多步骤计划完成；正常动作和可本地恢复故障不逐步调用模型。

### B01：BeliefWorldState

**目标：** 将多次观察形成带时效和置信度的任务信念。  
**文件：** 新建 `applications/minecraft/world_state.py`、`tests/minecraft/test_world_state.py`  
**前置依赖：** A01  
**实施步骤：** 合并资源位置、探索区域、失败路径、安全点和工作台；每条易失事实保存 observed/expires/confidence/source；静态规则只引用 Catalog。  
**非目标：** 不调用模型、不保存完整区块。  
**验证命令：** `pytest -q tests/minecraft/test_world_state.py`  
**验收结果：** 新观察覆盖旧事实，过期事实降权/移除，Catalog 未被复制。  
**失败诊断：** 检查实体键、时钟注入和过期策略。

### B02：类型化 TaskPlan 与计划版本

**目标：** 表达滚动计划、前置条件、成功判据和 fallback。  
**文件：** 修改 `applications/minecraft/models.py`，新建 `tests/minecraft/test_plan_models.py`  
**前置依赖：** A01  
**实施步骤：** 定义 PlanStep/TaskPlan/PlanSource；加入 plan version、策略理由、触发事件和禁止任意代码的严格 schema。  
**非目标：** 不在 DTO 内执行计划或模型。  
**验证命令：** `pytest -q tests/minecraft/test_plan_models.py`  
**验收结果：** 合法计划 round-trip；未知动作、循环引用载荷和额外代码字段失败。  
**失败诊断：** 检查 discriminated union、版本单调性和 extra fields。

### B03：Minecraft Memory Adapter

**目标：** 预算化复用 TaleClaw Memory。  
**文件：** 新建 `applications/minecraft/memory_adapter.py`、`tests/minecraft/test_memory_adapter.py`  
**前置依赖：** B01  
**实施步骤：** 组合 `ScopedMemoryStore`/可选历史向量索引；分类读写位置、任务、失败策略、成功计划和技能经验；只暴露按目标与预算检索。  
**非目标：** 不逐轮读取 `MemoryStore.read_all()`，不新建第二个通用 Memory 系统。  
**验证命令：** `pytest -q tests/minecraft/test_memory_adapter.py`  
**验收结果：** 初始/重规划查询返回相关有限内容，普通动作路径零读取。  
**失败诊断：** 检查 user scope、检索预算和测试临时目录。

### B04：Planner/Critic Context Builder

**目标：** 构建无聊天历史污染的领域模型上下文。  
**文件：** 新建 `applications/minecraft/context.py`、`tests/minecraft/test_context.py`  
**前置依赖：** B01, B03  
**实施步骤：** 实现 `MinecraftPlannerContextBuilder`、`MinecraftCriticContextBuilder`；复用 `ContextBudgeter`、prompt assets 和 Memory adapter；按 section 裁剪目标、信念、Catalog、事件和失败。  
**非目标：** 不调用普通 `ContextBuilder.build()` 注入整段 Session.messages。  
**验证命令：** `pytest -q tests/minecraft/test_context.py`  
**验收结果：** 上下文在预算内、含相关记忆、无普通聊天历史和凭证。  
**失败诊断：** 检查 section 预算、source 标记和裁剪顺序。

### B05：ModelTaskRunner 结构化调用适配

**目标：** Planner/LLMCritic 直接复用现有模型任务接口。  
**文件：** 新建 `applications/minecraft/model_gateway.py`，必要时向后兼容修改 `models/model_task_runner.py`，新建 `tests/minecraft/test_model_gateway.py`  
**前置依赖：** B02, B04  
**实施步骤：** 为 planner/revision/critic 配置独立 AgentSpec purpose；增加严格 JSON 解析、超时、usage/token 元数据、trace callback 和失败分类；保留现有 `ModelTaskRunner.run()` 返回字符串行为。  
**非目标：** 不创建模型池、不经过 AgentLoop、不为 Minecraft 实现通用推理循环。  
**验证命令：** `pytest -q tests/minecraft/test_model_gateway.py tests/test_model_task_runner.py`  
**验收结果：** 三类模型任务可区分配置；超时/无效 JSON 可诊断；现有 runner 测试无回归。  
**失败诊断：** 检查 provider response usage、兼容签名和超时线程/协程清理。

### B06：Planner 与 fallback plan

**目标：** 生成石镐的滚动分层计划。  
**文件：** 新建 `applications/minecraft/planner.py`、`tests/minecraft/test_planner.py`  
**前置依赖：** B02, B05  
**实施步骤：** 根据目标、BeliefWorldState、Catalog、Memory 和预算生成短窗口计划；实现修订入口和无模型保守 fallback；所有调用要求 Gate approval 参数。  
**非目标：** Planner 不自行判断何时调用模型，不执行动作。  
**验证命令：** `pytest -q tests/minecraft/test_planner.py`  
**验收结果：** 石镐计划可动态跳过已有材料；缺少批准决定时禁止模型调用。  
**失败诊断：** 检查 approval token、计划窗口和模型/fallback source。

### B07：PlanValidator

**目标：** 确定性验证模型计划。  
**文件：** 新建 `applications/minecraft/plan_validator.py`、`tests/minecraft/test_plan_validator.py`  
**前置依赖：** A02, B02  
**实施步骤：** 检查动作白名单、参数、依赖环、工具等级、安全 profile、预算和成功判据；输出结构化 rejection。  
**非目标：** 不替 Planner 选择正常策略，不调用模型。  
**验证命令：** `pytest -q tests/minecraft/test_plan_validator.py`  
**验收结果：** 合法多步计划通过，越权动作、循环、超预算和错误工具等级被拒绝。  
**失败诊断：** 检查 DAG 遍历、Catalog 查询和安全 profile 注入。

### B08：LocalEvaluator

**目标：** 对动作结果进行完全确定性评估。  
**文件：** 新建 `applications/minecraft/local_evaluator.py`、`tests/minecraft/test_local_evaluator.py`  
**前置依赖：** A03, B02  
**实施步骤：** 比较动作前后观察、ActionResult、PlanStep、目标、预算和重试；输出 continue/step_succeeded/local_recovery/escalate/completed/blocked/failed。  
**非目标：** 不调用模型、不直接修改计划、不处理逐 tick 事件。  
**验证命令：** `pytest -q tests/minecraft/test_local_evaluator.py`  
**验收结果：** 已知失败产生本地恢复，真实背包满足目标才 completed，无进展可升级。  
**失败诊断：** 检查观察 diff、错误分类和重试计数归属。

### B09：ReasoningGate

**目标：** 成为所有认知调用的唯一确定性门控。  
**文件：** 新建 `applications/minecraft/reasoning_gate.py`、`tests/minecraft/test_reasoning_gate.py`  
**前置依赖：** B02, B08  
**实施步骤：** 定义 CognitiveDecision；实现事件去重/聚合/防抖/冷却、同类失败、预算消费、安全绕过、重复策略排除和 plan version 校验；首次 Planner 也必须审批。  
**非目标：** 不调用模型、不执行 Bridge 动作、不把里程碑本身视为模型触发。  
**验证命令：** `pytest -q tests/minecraft/test_reasoning_gate.py`  
**验收结果：** 逐 tick 永不触发；重复事件只决策一次；安全直接抢占；预算耗尽只允许 fallback/blocked/failed。  
**失败诊断：** 检查 event ID 持久集合、冷却时钟、原子预算和 plan version。

### B10：LLMCritic

**目标：** 只对升级事件进行高层策略分析。  
**文件：** 新建 `applications/minecraft/llm_critic.py`、`tests/minecraft/test_llm_critic.py`  
**前置依赖：** B05, B09  
**实施步骤：** 接受 Gate 的 `CALL_LLM_CRITIC` 决定；分析复杂偏差、多个策略失败和长期低效；输出类型化 critique/revision request 并记录 trace metadata。  
**非目标：** 不在每个动作或每个里程碑后调用，不直接执行动作。  
**验证命令：** `pytest -q tests/minecraft/test_llm_critic.py`  
**验收结果：** 未获 Gate 批准时零模型调用；合法升级只调用一次并携带触发 event/plan version。  
**失败诊断：** 检查 decision 类型、上下文 builder 和重复事件保护。

### B11：Worker 认知循环重构

**目标：** 将 Phase A Worker 升级为领域专用智能循环。  
**文件：** 修改 `applications/minecraft/worker.py`、`service.py`，新建 `applications/minecraft/safety_controller.py`、`tests/minecraft/test_worker_reasoning.py`  
**前置依赖：** B01, B06–B10  
**实施步骤：** 按观察→世界更新→安全→LocalEvaluator→ReasoningGate→必要时 Planner/LLMCritic→动作→验证运行；所有模型预算由 Gate 消费；普通合法步骤直接继续。  
**非目标：** 不依赖 AgentLoop；不实现模型池、ToolRegistry、Memory store、SessionStore、TraceStore、CancellationRegistry 或 MessageBus。  
**验证命令：** `pytest -q tests/minecraft/test_worker_reasoning.py`  
**验收结果：** 正常步骤和本地恢复零模型调用；恢复耗尽只升级一次；SafetyController 无模型立即抢占。  
**失败诊断：** 检查职责顺序、Gate 绕过路径、动作后真实观察和组件注入。

### B12：石镐智能 E2E

**目标：** 验收 Phase B 的动态多步规划。  
**文件：** 新建 `tests/minecraft/test_stone_pickaxe_e2e.py`  
**前置依赖：** B11, A06  
**实施步骤：** Fake 世界执行“制作一把石镐”；覆盖已有材料跳步、本地路径恢复、滚动计划耗尽扩展、非法模型输出修订/fallback。  
**非目标：** 不连接真实服务器、不要求 PostgreSQL。  
**验证命令：** `pytest -q tests/minecraft/test_stone_pickaxe_e2e.py`  
**验收结果：** 最终背包真实存在石镐；调用次数与第 5 节决策表一致。  
**失败诊断：** 检查 fake model 脚本、plan version、ReasoningGate event 去重和背包真值。

## 8. Phase C：TaleClaw Runtime 集成

**阶段目标：** 聊天、程序化 API、CLI 和 Benchmark 创建同一种 MinecraftTask；AgentLoop 只是可选入口。

### C01：抽取 Kernel 依赖容器

**目标：** 让多个 Application 复用 `build_runtime()` 创建的同一批实例。  
**文件：** 新建 `runtime/services.py`，修改 `runtime/bootstrap.py`、`tests/minecraft/test_kernel_services.py`  
**前置依赖：** A04；可与 Phase B 的领域组件并行  
**实施步骤：** 定义 `RuntimeServices` 容器，承载 ModelPool/ModelTaskRunner、PluginManager 装配所需对象、ToolRegistry/Executor、Memory、ContextBudgeter/Builder、SessionManager、TraceStore、CancellationRegistry、MessageBus；保持 `build_runtime()` 公共返回兼容。  
**非目标：** 不把 AgentLoop 放进 Kernel 容器，不改变 Runtime 通用模型循环语义。  
**验证命令：** `pytest -q tests/minecraft/test_kernel_services.py tests/test_config_bootstrap.py`  
**验收结果：** Chat/Coding/Minecraft 注入对象 identity 一致，bootstrap 关闭 Minecraft 时无行为变化。  
**失败诊断：** 检查构建顺序、PluginManager 注册时机和循环依赖。

### C02：AgentLoop/AppRuntime 接入共享取消

**目标：** 用同一 CancellationRegistry 支持聊天回合和 MinecraftTask。  
**文件：** 修改 `runtime/agent_loop.py`、`runtime/app_runtime.py`、现有取消测试、`tests/minecraft/test_cancellation.py`  
**前置依赖：** A04, C01  
**实施步骤：** 替换 AgentLoop 私有 event map；保持 `request_cancel(session_id)` 兼容；为 task scope 提供独立取消方法且不在聊天回合结束时误释放任务 token。  
**非目标：** 不让 Worker 调用 AgentLoop.request_cancel。  
**验证命令：** `pytest -q tests/test_agent_loop_phases.py tests/minecraft/test_cancellation.py`  
**验收结果：** 两类 scope 相互隔离；取消传播到 Worker；现有聊天取消不回归。  
**失败诊断：** 检查 scope namespace、finally 清理和 AppRuntime 转发。

### C03：Minecraft Trace adapter 与事件目录

**目标：** 复用 TraceStore 记录长期任务和每次认知原因。  
**文件：** 新建 `applications/minecraft/tracing.py`，修改 `runtime/trace/events.py`，新建 `tests/minecraft/test_tracing.py`  
**前置依赖：** A01, C01  
**实施步骤：** 用 `RunState`/`TraceStore.append_event` 适配第 4.8 节事件；关联 task/run/session/plan/event/action；模型事件记录预算、上下文规模和 Validator 结果。  
**非目标：** 不新建第二套 trace 文件格式或日志后端。  
**验证命令：** `pytest -q tests/minecraft/test_tracing.py tests/test_run_trace.py`  
**验收结果：** 可回答每次模型调用为何发生且 trace 订阅仍工作。  
**失败诊断：** 检查 run lifecycle、trace_only metadata、敏感字段过滤和并发写。

### C04：Minecraft Memory/Context Kernel 装配

**目标：** 将 B03/B04 接到共享 Memory 与 Context 服务。  
**文件：** 修改 `runtime/bootstrap.py`、`applications/minecraft/memory_adapter.py`、`context.py`、`tests/minecraft/test_kernel_context_memory.py`  
**前置依赖：** B03, B04, C01  
**实施步骤：** 注入同一 `ScopedMemoryStore`、可选向量索引、ContextBudgeter 和 prompt assets；按 user/task scope 检索；重要 checkpoint 才写经验。  
**非目标：** 不复制 Memory 文件，不把普通 Session.messages 作为 Planner 历史。  
**验证命令：** `pytest -q tests/minecraft/test_kernel_context_memory.py`  
**验收结果：** identity/scope 正确，普通动作无检索，规划上下文满足预算。  
**失败诊断：** 检查 user scope resolver、服务可见性和后台 Memory 生命周期。

### C05：Session 关联与 ProgressPublisher

**目标：** 建立 UserSession→Task→Run 关系并复用 MessageBus 发用户进度。  
**文件：** 新建 `applications/minecraft/session_adapter.py`、`progress.py`、`tests/minecraft/test_session_progress.py`  
**前置依赖：** A12, C01  
**实施步骤：** Session 只保存 task ID/摘要关联；完整世界 checkpoint 留 Store；将节流后的领域事件转成 `OutboundMessage`；无聊天入口时使用 null publisher。  
**非目标：** 不把 MessageBus 当内部领域 EventBus，不向 Session metadata 写完整世界。  
**验证命令：** `pytest -q tests/minecraft/test_session_progress.py`  
**验收结果：** 用户只收到节流进度；API/Benchmark 无 MessageBus 也能运行。  
**失败诊断：** 检查 async publish 调度、channel/chat 映射和 metadata 大小。

### C06：Minecraft AgentSpec 与显式 tool mode

**目标：** 为聊天入口提供最小且隔离的 Agent 定义。  
**文件：** 新建 `agents/minecraft.py`，修改 `agents/__init__.py`、`tools/tool_registry.py`，新建 `tests/minecraft/test_agent_spec.py`  
**前置依赖：** C01  
**实施步骤：** 定义 `MINECRAFT_AGENT_SPEC` 和 minecraft mode；只允许四个外部 task 工具；禁用 shell、文件、子 Agent 和内部 Bridge 动作工具化。  
**非目标：** AgentSpec 不承担长期 Worker 循环。  
**验证命令：** `pytest -q tests/minecraft/test_agent_spec.py tests/test_tool_safety.py`  
**验收结果：** Minecraft mode 只看见外部工具，bot/coding 权限不变。  
**失败诊断：** 检查 `_modes_for_tool`、allowed_agents 和 plugin 注册顺序。

### C07：MinecraftPlugin

**目标：** 将聊天工具映射到同一 MinecraftTaskService。  
**文件：** 新建 `plugins/minecraft/__init__.py`、`plugin.py`，修改 `plugins/__init__.py`，新建 `tests/minecraft/test_plugin.py`  
**前置依赖：** A12, C05, C06  
**实施步骤：** 注册 start/status/cancel/bot status；传递 session/user；只调用 Service；返回有限结构化结果。  
**非目标：** Plugin 不执行 Worker 循环、不轮询 Bridge、不直接调用 Planner。  
**验证命令：** `pytest -q tests/minecraft/test_plugin.py tests/test_plugin_manager.py`  
**验收结果：** 四工具经过现有 ToolRegistry/Executor；跨 session 查询/取消被拒绝。  
**失败诊断：** 检查 PluginContext 能力、session_scoped handler 参数和输出脱敏。

### C08：程序化 API、CLI 与 Benchmark 入口

**目标：** 完全绕过 AgentLoop 创建和管理同一种 Task。  
**文件：** 新建 `applications/minecraft/api.py`、`scripts/minecraft_task.py`、`benchmarks/minecraft_resource.py`、对应 tests  
**前置依赖：** A12, C01  
**实施步骤：** API facade 接收 CreateRequest；CLI 支持 start/status/cancel；Benchmark 注入 Fake Bridge/Store/Model；三者使用 bootstrap 提供的同一 Service factory。  
**非目标：** 不新增独立模型池、HTTP 公网服务或自动连接未配置服务器。  
**验证命令：** `pytest -q tests/minecraft/test_direct_entrypoints.py`  
**验收结果：** monkeypatch AgentLoop 失败时 API/CLI/Benchmark 仍可完成 Fake 任务。  
**失败诊断：** 检查依赖注入、命令参数和进程内 worker 生命周期。

### C09：显式 `/minecraft` 入口

**目标：** MVP 聊天入口无需自动意图分类。  
**文件：** 修改 `runtime/routing/intent.py`、`execution_plan.py`、`agent_router.py`、相关 routing tests  
**前置依赖：** C06, C07  
**实施步骤：** 识别 `/minecraft <目标>` 或 metadata `application_mode=minecraft`；映射 AgentSpec；保留 bot/coding/hybrid；命令正文传给 Plugin。  
**非目标：** 不在本任务实现模糊自然语言自动分类。  
**验证命令：** `pytest -q tests/minecraft/test_explicit_routing.py tests/test_hybrid_mode_routing.py tests/test_mode_switch.py`  
**验收结果：** 显式入口稳定路由，普通 Minecraft 问答仍走 bot。  
**失败诊断：** 检查 command 剥离、ExecutionPlanner 分支和 session active_agent。

### C10：可选确定性自动意图识别

**目标：** 在不阻塞 MVP 的前提下识别明确资源命令。  
**文件：** 修改 `runtime/routing/intent.py`、`hybrid_classifier.py`、routing tests  
**前置依赖：** C09  
**实施步骤：** 功能开关控制；只接受 parser 高置信单资源目标；问答/代码请求不命中；无效 hybrid 输出回退 bot。  
**非目标：** 不把自动分类作为 A/B/C 主验收前置条件。  
**验证命令：** `pytest -q tests/minecraft/test_optional_routing.py tests/test_hybrid_mode_routing.py`  
**验收结果：** 关闭时仅显式入口；开启时明确命令命中且无已知误判。  
**失败诊断：** 检查分类优先级、feature flag 和 coding 强意图。

### C11：Bootstrap 与 Runtime 集成 E2E

**目标：** 验证三入口共享 Kernel 和 TaskService。  
**文件：** 修改 `runtime/bootstrap.py`、`runtime/app_runtime.py`，新建 `tests/minecraft/test_runtime_integration.py`  
**前置依赖：** C02–C09；C10 可选  
**实施步骤：** 按开关装配 MinecraftApplication/Plugin；测试聊天、API、CLI/Benchmark 路径；检查对象 identity 和 Worker 依赖图。  
**非目标：** 不要求 AgentLoop 驱动 Worker，不连接真实服务器。  
**验证命令：** `pytest -q tests/minecraft/test_runtime_integration.py tests/test_config_bootstrap.py`  
**验收结果：** Worker 不重复实现通用模型、工具、Session、Memory、Context、Trace、事件和取消基础设施，但拥有 Minecraft 领域任务状态循环；API/Benchmark 完全绕过 AgentLoop。  
**失败诊断：** 检查 bootstrap factory、共享实例、Plugin tool handler 和后台 worker 清理。

## 9. Phase D：生产化与长期任务

**阶段目标：** 可持久查询、取消、恢复长任务，扩展至五类资源与“净新增 30 个钻石”。

### D01：PostgresMinecraftTaskStore schema

**目标：** 在不改变 Store 调用方的情况下增加 PostgreSQL。  
**文件：** 新建 `applications/minecraft/stores/postgres.py`、`tests/minecraft/test_postgres_store_schema.py`  
**前置依赖：** A05  
**实施步骤：** 使用 `runtime.db` 建 task/run/event/checkpoint 表；NOT NULL 安全回填；幂等迁移；活动 bot 部分唯一索引。  
**非目标：** 不修改现有 sessions 表语义，不删除历史数据。  
**验证命令：** `pytest -q tests/minecraft/test_postgres_store_schema.py`  
**验收结果：** 空库和重复升级通过，所有新增非空列有迁移策略。  
**失败诊断：** 检查 PostgreSQL DSN、事务提交、information_schema 和旧 schema fixture。

### D02：PostgreSQL CRUD、事件与 checkpoint

**目标：** 完整实现 Store Protocol。  
**文件：** 修改 `applications/minecraft/stores/postgres.py`、新建 `tests/minecraft/test_postgres_store.py`  
**前置依赖：** D01  
**实施步骤：** 实现幂等创建、乐观更新、事件去重、checkpoint 版本和 recoverable 查询；JSON 大小与敏感字段校验。  
**非目标：** 不让 Service/Worker 使用 psycopg 类型。  
**验证命令：** `pytest -q tests/minecraft/test_postgres_store.py`  
**验收结果：** 与 InMemory contract tests 一致，重复事件和并发更新安全。  
**失败诊断：** 检查事务隔离、唯一约束映射和 JSON 编解码。

### D03：Worker lease 与单 bot 活动任务

**目标：** 防止多进程重复控制同一机器人。  
**文件：** 修改 Store Protocol/两种 Store、`applications/minecraft/worker.py`、新建 `tests/minecraft/test_worker_lease.py`  
**前置依赖：** D02  
**实施步骤：** 增加 lease owner/expiry/heartbeat/条件续约；获取失败不执行动作；数据库约束兜底单活动任务。  
**非目标：** 不实现分布式队列或多 bot 调度器。  
**验证命令：** `pytest -q tests/minecraft/test_worker_lease.py`  
**验收结果：** 两 worker 竞争只有一个提交动作，过期 lease 可安全接管。  
**失败诊断：** 检查数据库时钟、续约竞态和终态释放。

### D04：启动恢复与身份校验

**目标：** 重启后安全恢复而非误报完成。  
**文件：** 修改 `applications/minecraft/service.py`、`worker.py`、新建 `tests/minecraft/test_resume.py`  
**前置依赖：** D02, D03  
**实施步骤：** 扫描 recoverable；重连并先观察；校验 bot/server/world/baseline/plan version；一致才恢复，不一致 blocked。  
**非目标：** 不跨服务器迁移任务，不猜测死亡丢失前的背包。  
**验证命令：** `pytest -q tests/minecraft/test_resume.py`  
**验收结果：** 一致 checkpoint 继续；身份或世界变化阻塞；当前背包重新决定进度。  
**失败诊断：** 检查 world identity 来源、checkpoint 原子性和 lease 接管。

### D05：完整故障分类与恢复控制器

**目标：** 有界处理路径、工具、背包、断线和死亡故障。  
**文件：** 新建 `applications/minecraft/recovery.py`，修改 Worker/ReasoningGate/LocalEvaluator，新增 recovery tests  
**前置依赖：** B08–B11, D04  
**实施步骤：** 定义本地恢复策略与次数；已知可恢复错误不调用模型；恢复耗尽只产生一个升级事件；断线/死亡后重新观察。  
**非目标：** 不无限重试、不绕过服务器拒绝或反作弊。  
**验证命令：** `pytest -q tests/minecraft/test_recovery.py`  
**验收结果：** 每类故障进入明确恢复/阻塞/失败路径，同类失败上限有效。  
**失败诊断：** 检查 error taxonomy、Gate event key 和恢复后 plan validity。

### D06：五资源 Catalog 与生存动作

**目标：** 扩展原木、圆石、煤、铁原矿和钻石科技树。  
**文件：** 修改 Catalog；新增 Bridge `actions/basic.js`、扩展 `actions/collect.js`、对应 Python/Node tests  
**前置依赖：** A10, B07  
**实施步骤：** 加配方、掉落、工具等级、普通/深层矿；实现 craft/equip/eat/return_safe；铁镐门槛双层校验。  
**非目标：** 不实现战斗、容器、村民、附魔、下界或建筑。  
**验证命令：** `pytest -q tests/minecraft/test_catalog.py && (cd minecraft-bridge && npm test -- basic-actions.test.js collect-actions.test.js)`  
**验收结果：** 五资源定义齐全，木/石镐挖钻石在 Python 与 Bridge 都被拒绝。  
**失败诊断：** 检查 minecraft-data 版本映射、配方 API 和物品规范名。

### D07：安全分支采矿

**目标：** 为地下资源提供有界高层策略。  
**文件：** 新建 `minecraft-bridge/src/actions/branch-mine.js`、`test/branch-mine.test.js`  
**前置依赖：** A09, D06  
**实施步骤：** 白名单隧道形状、长度和挖掘预算；逐块检查熔岩、坠落、氧气、生命、饥饿和工具；发现目标/危险/取消即停止。  
**非目标：** 不允许任意坐标大范围挖掘或模型逐方块控制。  
**验证命令：** `cd minecraft-bridge && npm test -- branch-mine.test.js`  
**验收结果：** 目标发现、安全中止、预算耗尽和取消均返回稳定结果。  
**失败诊断：** 检查危险扫描时机、AbortSignal 和块预算原子扣减。

### D08：Bridge 生产认证与监控可替换性

**目标：** 强化远程部署边界并证明 watcher 可替换。  
**文件：** 修改 Bridge server/config、Python BridgeClient，新增 auth/watch contract tests  
**前置依赖：** A10, A11  
**实施步骤：** Bearer auth、受信远程 allowlist、请求/并发限制、脱敏；保留 polling 实现；用 fake SSE watcher 通过同一 Protocol contract。  
**非目标：** 本阶段不要求实际实现 SSE/WebSocket 服务端。  
**验证命令：** `pytest -q tests/minecraft/test_action_watch_contract.py && (cd minecraft-bridge && npm test -- server-auth.test.js)`  
**验收结果：** Worker 不感知 transport；未授权/越权地址失败；凭证不进日志。  
**失败诊断：** 检查 URL 解析、代理变量、header 脱敏和 async iterator 取消。

### D09：长期取消、预算与无后续动作

**目标：** 在持久任务中保证取消和预算终止。  
**文件：** 修改 Service/Worker/Store/ReasoningGate，新增 long-task cancellation tests  
**前置依赖：** C02, D02–D05  
**实施步骤：** 先持久化 cancel_requested，再请求共享 token/Bridge cancel；重启读取取消；模型/动作/时间预算原子消费。  
**非目标：** 不以进程 kill 代替动作取消。  
**验证命令：** `pytest -q tests/minecraft/test_long_task_cancellation.py tests/minecraft/test_reasoning_budget.py`  
**验收结果：** 取消后零后续动作；模型预算耗尽后零后续模型调用。  
**失败诊断：** 检查取消写入顺序、watch_action 退出和 Gate 预算事务。

### D10：配置、Compose 与运维文档

**目标：** 提供可选生产部署路径。  
**文件：** 修改 `.env.example`、`docker-compose.yml`、`docs/README.md`，新建 `minecraft-bridge/Dockerfile`、`docs/minecraft-agent.md`  
**前置依赖：** C11, D08  
**实施步骤：** 功能开关、服务器/Bridge/预算配置；可选 minecraft profile；记录显式入口、CLI、状态/取消、离线模式责任和故障排查。  
**非目标：** 不默认启动 Bridge、不把真实凭证写入仓库。  
**验证命令：** `docker compose config && docker compose --profile minecraft config && pytest -q tests/minecraft/test_config_docs.py`  
**验收结果：** 默认 compose 不启用 Minecraft；profile 配置合法且变量名与代码一致。  
**失败诊断：** 检查 YAML interpolation、容器 DNS、loopback/服务名差异和文档命令。

### D11：“净新增 30 个钻石”长程 E2E

**目标：** 验证完整智能、恢复和科技树。  
**文件：** 新建 `tests/minecraft/test_diamond_e2e.py`、扩展 Fake Bridge  
**前置依赖：** B12, D04–D09  
**实施步骤：** 空背包准备木/石/铁工具；动态发现资源缩短计划；覆盖一次可恢复故障、两类钻石矿和 checkpoint；以当前背包净新增 30 完成。  
**非目标：** 不把真实服务器钻石成功作为 CI 前置条件。  
**验证命令：** `pytest -q tests/minecraft/test_diamond_e2e.py`  
**验收结果：** succeeded、净新增 30、模型调用符合 Gate 表、恢复不重复动作。  
**失败诊断：** 检查科技依赖、fake 掉落、plan version、checkpoint 和背包基线。

### D12：全量回归、安全扫描与可选真实 smoke

**目标：** 完成生产化验收。  
**文件：** 全部本功能文件与现有关键回归；更新 `scripts/minecraft_smoke.py`  
**前置依赖：** D01–D11  
**实施步骤：** Python/Node 全测；Runtime 路由/Plugin/Session/Trace 回归；凭证扫描；smoke 仅显式 `--connect` 连接专用离线服务器并先做原木任务。  
**非目标：** 不连接未配置服务器、不自动执行真实钻石任务。  
**验证命令：** `python -m compileall applications/minecraft plugins/minecraft runtime/cancellation.py && pytest -q && (cd minecraft-bridge && npm ci && npm test)`  
**验收结果：** 全部自动化测试无外部网络通过；无凭证；可选 smoke 正确成功或诊断阻塞。  
**失败诊断：** 按 Python、Node、PostgreSQL、Runtime 回归、凭证扫描和真实服务器六类分别定位。

## 10. 新依赖图

```text
Phase A（可独立交付）
A01 DTO/Ports ─┬→ A02 Catalog/Parser ─→ A03 Progress
               ├→ A05 InMemory Store ───────┐
               └→ A06 Fake Bridge ──────────┤
A04 Cancellation ─→ A11 BridgeClient ───────┤
A07 Node Scaffold → A08 Observation ─┐      │
                    A09 Safety/Action ├→ A10│
                                      └──────┤
                                             ▼
                                      A12 Wood E2E

Phase B（依赖 Phase A；领域智能）
B01 BeliefWorldState ─┬→ B03 Memory → B04 Context → B05 Model Gateway → B06 Planner
B02 TaskPlan ─────────┼───────────────────────────────────────────────→ B07 Validator
                      └→ B08 LocalEvaluator → B09 ReasoningGate → B10 LLMCritic
                                     B06/B07/B08/B09/B10 → B11 Worker → B12 Pickaxe E2E

Phase C（Kernel 适配可与部分 Phase B 并行）
A04 → C01 Kernel Services → C02 Cancellation
                         ├→ C03 Trace
                         ├→ C04 Memory/Context
                         ├→ C05 Session/Progress
                         └→ C06 AgentSpec → C07 Plugin
A12/C01 → C08 API/CLI/Benchmark
C06/C07 → C09 Explicit Routing → C10 Optional Auto Routing
C02–C09 → C11 Runtime Integration

Phase D（不阻塞 MVP）
A05 → D01 Postgres → D02 Events/Checkpoint → D03 Lease → D04 Resume → D05 Recovery
A10/B07 → D06 Five Resources → D07 Branch Mining
A10/A11 → D08 Auth/Watch
C02/D02–D05 → D09 Long Cancellation/Budgets
C11/D08 → D10 Deployment
B12/D04–D09 → D11 Diamond E2E
D01–D11 → D12 Full Validation
```

Node Bridge 的 A07–A10 与 Python 领域 A01–A06/A11 可并行。Fake Bridge A06 必须先于 A12/B12/D11 的复杂 E2E。所有自动化测试默认不访问外部网络，真实服务器只用于显式 smoke。

## 11. 原 T1～T61 迁移对照

| 原任务 | 新任务 | 处理 |
|--------|--------|------|
| T1 | A04 | 保留：抽取 CancellationRegistry |
| T2 | C02 | 延后到 Runtime 集成：AgentLoop 接共享取消 |
| T3 | C02 | 合并：AppRuntime 取消转发 |
| T4 | C06 | 延后：minecraft tool mode |
| T5 | A01 | 保留并收敛为基础 DTO |
| T6 | A01 | 合并：Observation/Action DTO |
| T7 | B02 | 延后：类型化计划 DTO |
| T8 | A02、D06 | 拆分：Phase A 原木 Catalog；Phase D 五资源 |
| T9 | A02 | 保留：显式目标 Parser |
| T10 | A03 | 保留：净新增计数 |
| T11 | A03 | 合并：基础状态机 |
| T12 | B01 | 修订：明确 BeliefWorldState |
| T13 | B04、B05、B06 | 拆分：Context、模型适配、Planner |
| T14 | B07 | 保留：PlanValidator |
| T15 | B06、B07、B09 | 拆分：修订/fallback 受 Gate 控制 |
| T16 | B08、B10 | 必需拆分：LocalEvaluator 与 LLMCritic |
| T17 | B09 | 重写：预算、去重、冷却统一由 ReasoningGate |
| T18 | D01 | 延后：PostgreSQL schema |
| T19 | D01、D02 | 延后并拆分：事件/checkpoint |
| T20 | A05、D02 | 拆分：先 InMemory contract，后 PostgreSQL |
| T21 | A05、D02 | 拆分：内存与持久 checkpoint |
| T22 | A11、D08 | 拆分：MVP client 与生产认证 |
| T23 | A08、A11 | 拆分：Bridge 观察与 Python 解析 |
| T24 | A09、A11、D08 | 拆分：动作取消与可替换 watch |
| T25 | A12、C05 | 拆分：Service 基线与 Session 关联 |
| T26 | A12、B11 | 重写：MinecraftWorker 是领域循环 |
| T27 | B11 | 重写：经 ReasoningGate 接 Planner/LLMCritic |
| T28 | D05 | 延后：完整安全抢占与恢复 |
| T29 | A04、A12、C02、D09 | 拆分：MVP 与长期取消传播 |
| T30 | D04 | 延后：启动恢复 |
| T31 | A03 | 保留：终态报告 |
| T32 | C06 | 延后：AgentSpec 只是聊天入口 |
| T33 | C09、C10 | 延后：先显式入口，再可选自动识别 |
| T34 | C09、C10 | 延后：ExecutionPlanner/Router |
| T35 | C07 | 保留：Plugin 外部工具 |
| T36 | C01、C11 | 拆分：Kernel 容器与最终装配 |
| T37 | C11 | 重写：允许领域 Worker，禁止复制 Kernel |
| T38 | A07 | 保留：Bridge 包初始化 |
| T39 | A07、D08 | 拆分：本机默认与生产远程认证 |
| T40 | A07 | 保留：Bridge schema |
| T41 | A09 | 保留：动作幂等 |
| T42 | A09、D07 | 拆分：restricted 安全与地下安全 |
| T43 | A08 | 保留：Bot 生命周期 |
| T44 | A08 | 保留：有限观察 |
| T45 | A10、D06 | 拆分：MVP stop/cancel 与生存动作 |
| T46 | A10、D06 | 拆分：先原木，再五资源 |
| T47 | D07 | 延后：分支采矿 |
| T48 | A07、D08 | 拆分：MVP 本机认证与生产强化 |
| T49 | A08、A10 | 保留并拆分：连接/状态 API |
| T50 | A10 | 保留：动作 API/入口 |
| T51 | A06 | 提前：Fake Bridge |
| T52 | D11 | 延后：30 钻石长程 E2E |
| T53 | D04、D05、D09 | 拆分：恢复、故障和长期取消 |
| T54 | A12、B12、C11 | 拆分到各阶段 E2E |
| T55 | C11、D12 | 拆分：Runtime 关键回归与全量回归 |
| T56 | A10、D12 | 拆分：Bridge MVP 与全量测试 |
| T57 | C01、D10 | 拆分：功能开关与生产配置 |
| T58 | D10 | 延后：Compose profile |
| T59 | D10 | 延后：运维文档 |
| T60 | A12、D12 | 拆分：无网络 check-only 与真实 smoke |
| T61 | D12 | 保留并延后：最终全量验证 |

没有静默删除原 T1～T61 的能力；它们被保留、拆分、合并或移动到不阻塞 MVP 的阶段。

## 12. Phase 任务数量

| Phase | 数量 | 可交付结果 |
|-------|-----:|------------|
| Phase A | 12 | 无 LLM/PostgreSQL 的“收集 4 个原木”闭环 |
| Phase B | 12 | ReasoningGate 控制的“制作一把石镐”智能闭环 |
| Phase C | 11 | 聊天/API/CLI/Benchmark 共享 TaskService 与 Kernel |
| Phase D | 12 | PostgreSQL 恢复、五资源和 30 钻石长任务 |
| 合计 | 47 | 分阶段交付，不再要求 61 项连续完成才形成 MVP |

ReasoningGate 位于 **B09**，LocalEvaluator 位于 **B08**，LLMCritic 位于 **B10**。

## 13. 架构自检

1. **MinecraftWorker 是否依赖 AgentLoop？** 否；它依赖领域组件和 Kernel ports。
2. **Planner 是否通过 AgentLoop 调用模型？** 否；它经 ModelGateway 直接调用 `ModelTaskRunner`。
3. **所有模型调用是否经过 ReasoningGate？** 是；首次规划、修订和 LLMCritic 都要求 Gate approval。
4. **普通动作完成是否会调用模型？** 否；LocalEvaluator 后继续下一合法步骤。
5. **SafetyController 是否可以不依赖模型立即抢占？** 是。
6. **LocalEvaluator 是否完全确定性？** 是。
7. **LLMCritic 是否只在升级事件中调用？** 是。
8. **Worker 是否复用了 TaleClaw 的模型、Memory、Context、Trace 和 Cancellation？** 是，通过 Kernel 实例与领域 adapter。
9. **API 和 Benchmark 是否可以绕过 AgentLoop？** 是。
10. **AgentLoop 是否只作为可选交互入口？** 是。
11. **Phase A 是否可以在没有 PostgreSQL 和 LLM 的情况下运行？** 是。
12. **自动化测试是否可以在没有真实 Minecraft Server 的情况下通过？** 是，使用 Fake Bridge/fake Mineflayer。
13. **Bridge 是否无法执行任意代码和原始协议动作？** 是，严格 schema 和 restricted profile 双重拒绝。
14. **当前背包是否为资源任务完成判断的唯一真值来源？** 是。
15. **文档是否清晰区分 Observation、BeliefWorldState 和 DomainCatalog？** 是，见 4.2。

## 14. 尚未解决的风险与明确非目标

### 风险

- `ModelTaskRunner` 当前只返回字符串，没有原生 usage/timeout/trace 结果；B05 必须以向后兼容方式适配，不能破坏现有调用方。
- `ContextBuilder` 目前面向聊天/编码上下文；B04/C04 复用其基础服务而不是强行调用聊天 build 流程，实施时需要注意依赖抽取边界。
- `MessageBus` 当前是用户消息总线；内部领域事件仍需 Store/Trace 承担，不能宣称已有通用 EventBus。
- Mineflayer 对服务器版本、插件组合和自定义规则的兼容性需要 fake adapter 与显式真实 smoke 双重验证。
- 后台 Worker 的 asyncio/thread 生命周期必须与当前同步 `ModelTaskRunner.run()` 协调，避免阻塞 AppRuntime 事件循环。
- PostgreSQL lease、取消和预算消费需要事务性条件更新，避免多进程竞态。

### 明确非目标

- Phase A 不使用 LLM、PostgreSQL、自动意图分类或完整科技树。
- 本轮不实现任何业务代码，也不修改本文件之外的文件。
- 不实现 Bedrock、Microsoft 正版登录、反作弊绕过、PvP、容器偷取、任意服务器命令或原始协议控制。
- 不在本设计中实现 SSE/WebSocket，只保留可替换 `watch_action` 边界。
- 不实现 builder/combat/cooperative 等安全模式，只预留 profile；MVP 仅实现 restricted resource mode。
- 不把真实服务器或真实钻石任务作为自动化测试前置条件。

## 15. 建议实施起点

最先实施 **A01：领域 DTO 与边界 Protocol**。它定义 Store、Bridge、Trace 和 Progress 的依赖方向，使 Node Bridge、Fake Bridge、InMemory Store 与 Worker 可以随后并行开发，同时从第一步就阻止 MinecraftWorker 依赖 AgentLoop 或 PostgreSQL。
