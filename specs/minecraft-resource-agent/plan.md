# Minecraft 资源任务 Agent 技术设计

## 1. 设计概览

本功能采用“现有 TaleClaw Runtime + Minecraft 领域应用 + 本地 Node.js Bridge”的三层结构：

```text
用户自然语言
    │
    ▼
TaleClaw AgentLoop / AgentRouter / Runtime
    │  复用 AgentSpec、模型路由、ToolRegistry/Executor、session、trace、cancel
    ▼
Minecraft Plugin Tools
    │  创建、查询、取消资源任务
    ▼
MinecraftTaskService（Python）
    │  持久化状态机、资源规划、预算、安全与恢复
    ▼
Minecraft Bridge（Node.js）
    │  受认证的高层 JSON 动作
    ▼
Mineflayer + pathfinder + collectblock
    │
    ▼
Minecraft Java 离线模式服务器
```

TaleClaw 仍只有一个通用 Agent 循环。Minecraft Agent 作为新的 `AgentSpec` 通过现有路由进入 `Runtime.run()`，再调用插件注册的高层工具。模型负责理解目标、建立世界状态、生成分层计划、选择策略、评估结果和动态重规划；领域执行器负责可靠地完成模型选定的高层动作。模型不等待每一个方块动作，也不接触 Mineflayer 原始对象、协议包或逐 tick 事件。

本设计不是把“挖钻石”编码成一条固定函数链。Agent 使用滚动规划：

```text
目标理解 → 观察世界 → 生成候选计划 → 约束校验 → 执行一个计划片段
                   ▲                         │
                   └──── 结果评估与重规划 ────┘
```

资源目录和规则只提供事实与边界，例如“钻石需要铁镐”“禁止岩浆和攻击玩家”；在边界内选择先探索、先补给、在哪里下矿、何时返回、如何绕开失败路径，由 Agent 根据当前世界状态决定。

## 2. 组件与职责

### 2.1 Minecraft Agent 定义

新增 `MINECRAFT_AGENT_SPEC`：

- `name="minecraft"`
- 使用现有聊天模型路由，不新增模型客户端。
- `ToolSet.mode="minecraft"`，只暴露 Minecraft 任务工具和必要的只读状态工具。
- 系统指令限定为资源任务，但明确赋予模型目标分解、候选策略比较、准备顺序选择、风险判断、进度反思和重规划职责。
- 模型可以创建带前置条件、成功判据和回退策略的计划，不被限制为预写死的唯一流程。
- 拒绝建筑、战斗、容器访问、服务器命令和任意代码。
- 禁止生成子 Agent；游戏执行不通过 coding/shell 工具。
- 每次认知回合设置明确预算；长期任务允许在重要状态变化时再次调用模型，而不是只在首次创建任务时调用一次。

修改 `AgentRouter` 的确定性意图识别，优先匹配结构清晰的资源指令，例如“挖 30 个钻石”“收集 16 个铁”。边界或含糊输入才使用现有 hybrid classifier，并把 `minecraft` 加入允许的分类结果。Coding 与普通聊天路由保持原行为。

### 2.2 Minecraft 插件

新增 `MinecraftPlugin`，通过现有 `PluginManager` 注册以下工具：

- `minecraft_start_resource_task(resource, quantity)`：校验目标，创建唯一活动任务，记录背包基线，返回任务 ID。
- `minecraft_task_status(task_id=None)`：返回阶段、净新增、当前动作、预算、重试和最近错误。
- `minecraft_cancel_task(task_id=None)`：写入取消请求并中止 Bridge 当前动作。
- `minecraft_bot_status()`：返回连接、版本、位置和有限生存状态。

所有工具：

- 仅允许 `minecraft` Agent 调用。
- 使用现有 ToolRegistry 授权、参数校验、ToolExecutor hooks 和 trace。
- 使用 session 上下文确定用户和会话归属，禁止跨会话查询或取消。
- 返回结构化、有限长度 JSON，不输出共享密钥、服务器凭证或无界世界数据。

插件 `setup()` 接收并组合已构建的共享服务，不创建 Runtime、模型池、session manager 或 trace store 的副本。

### 2.3 Python 领域应用

新增 `applications/minecraft/`，内部按职责拆分：

- `catalog.py`：中文资源别名、Minecraft 标识、矿石方块、掉落物、最低工具等级和准备链定义。
- `parser.py`：单资源、正整数目标的确定性解析和错误分类。
- `models.py`：任务、动作、观察、预算、报告等不可变 DTO；不引用 Node.js/Mineflayer 类型。
- `world_model.py`：把多次有限观察合并为任务级世界认知，包括已知资源位置、探索区域、失败路径、危险、可用材料和工具能力；只保存与当前目标有关的信息。
- `planner.py`：调用现有 `ModelTaskRunner` 生成和修订类型化分层计划，并提供确定性降级方案。
- `plan_validator.py`：验证计划中的动作、依赖、工具等级、安全约束、预算和成功判据，不替模型决定正常策略。
- `critic.py`：在里程碑、连续失败或环境突变时评估“继续、换策略、补给、撤退或阻塞”。
- `state_machine.py`：只管理任务生命周期、净新增计数、安全抢占、预算和终态，不硬编码完整采集流程。
- `service.py`：创建、恢复、查询、取消和单机器人互斥。
- `worker.py`：有界后台认知/执行循环；执行一个计划片段，吸收结果并按触发条件重新思考。
- `store.py`：PostgreSQL 领域任务与事件仓储。
- `bridge_client.py`：Bridge HTTP 客户端、认证、超时、幂等键和响应校验。
- `reporting.py`：成功、失败、取消和阻塞报告。

资源目录向模型提供可验证的事实图。例如钻石目标存在以下依赖事实：

```text
原木 → 木板/木棍/工作台 → 木镐 → 圆石 → 石镐
     → 铁原矿 → 熔炼所需条件 → 铁镐 → 钻石矿
```

这张图不是固定执行脚本。模型结合当前背包、附近资源、地形、时间、安全状态和历史失败生成计划：已有材料时跳步，铁不足时选择继续洞穴探索或安全分支采矿，食物不足时决定先补给或阻塞。模型可以改变计划顺序和搜索策略，但不得改写最低工具等级、安全禁令、完成判据或预算上限。

### 2.4 认知循环与智能边界

每个长期任务包含一个由现有 Runtime 模型能力驱动的认知循环：

1. **Goal Interpreter**：把自然语言目标转换成资源、数量、完成判据和用户约束。
2. **World Model Builder**：把当前观察与任务历史压缩为相关事实，不只看单次快照。
3. **Hierarchical Planner**：生成“阶段 → 子目标 → 高层动作”的计划 DAG，并为关键步骤附带前置条件、成功判据和回退策略。
4. **Plan Validator**：确定性检查动作白名单、安全、依赖和预算；不合法计划返回给模型修订。
5. **Executor**：只执行当前可执行的一个计划片段。
6. **Critic**：比较预期与实际结果，判断是否继续、局部修复或全局重规划。

触发模型重新思考的事件包括：

- 完成或跳过一个子目标。
- 新发现资源、洞穴、危险或更短路线。
- 当前动作失败，且同参数重试没有意义。
- 工具损坏、背包变化、生命/饥饿下降、死亡或重连。
- 搜索收益持续偏低，剩余预算不足以完成原计划。
- 用户补充约束、查询后继续，或修改/取消目标。

普通移动进度、挖掘动画和逐 tick 数据不会触发模型调用。这样智能发生在真正需要决策的节点，而动作稳定性仍由执行层保证。

### 2.5 Node.js Minecraft Bridge

新增独立的 `minecraft-bridge/` 包，使用：

- Mineflayer：连接 Java 服务器、版本协商、背包、制作、挖掘和事件。
- `mineflayer-pathfinder`：确定性寻路。
- `mineflayer-collectblock`：附近方块收集。
- `minecraft-data`：版本对应的方块、物品和配方元数据。

Bridge 默认只监听 `127.0.0.1`，通过 Bearer 共享密钥认证，限制正文大小、并发动作数和动作超时。它只提供高层 JSON API：

- `GET /health`
- `POST /v1/bot/connect`
- `POST /v1/bot/disconnect`
- `GET /v1/bot/state`
- `POST /v1/actions`
- `GET /v1/actions/{action_id}`
- `POST /v1/actions/{action_id}/cancel`

动作集合固定为：

- `observe`
- `move`
- `find_blocks`
- `collect_blocks`
- `pickup_drops`
- `craft`
- `equip`
- `eat`
- `branch_mine`
- `wait`
- `return_safe`
- `stop`

Bridge 接到动作后先执行 schema、工具等级、危险方块、距离和预算校验，再调用 Mineflayer。每个动作使用 `idempotency_key`；重复提交返回同一动作，不重复执行。Bridge 聚合高频事件，Python 只轮询动作摘要。

`branch_mine` 是受限领域动作，不等于任意挖掘：只允许白名单方块与安全隧道模式，持续检查熔岩、坠落、氧气、生命、饥饿、工具耐久和最大挖掘数。

## 3. Runtime 复用与必要重构

### 3.1 保持不变并直接复用

- `AgentLoop`：接收消息、读取/保存 session、执行路由和形成回复。
- `Runtime.run()`：执行 Minecraft Agent 的标准推理/工具循环。
- `AgentSpec`、模型池和 `ModelTaskRunner`：Agent 定义、模型选择、分层规划、Critic 和动态重规划。
- `ToolRegistry`、`ToolExecutor` 与现有 hooks：工具可见性、授权、执行和审计。
- `PluginManager`：Minecraft 工具生命周期。
- `SessionManager`：对话隔离与任务归属元数据。
- `TraceStore`：任务和动作的 trace 事件索引。
- PostgreSQL 连接与 SQL 兼容辅助层：不引入第二个数据库框架。

### 3.2 通用取消服务抽取

当前回合取消状态位于 `AgentLoop` 内部，不足以覆盖工具返回后仍运行的游戏任务。将其提取为 Runtime 级 `CancellationRegistry`：

- `AgentLoop` 和 `AppRuntime.request_cancel()` 改为使用同一个注册器，行为保持兼容。
- `MinecraftTaskService` 为长期任务注册独立 token。
- 用户取消时，插件先请求任务 token，再调用 Bridge 的动作取消接口。
- 启动任务的当前回合被取消时，未提交的任务不得继续启动。
- 进程重启后，以数据库中的 `cancel_requested_at` 恢复取消语义。

这是一项通用基础设施抽取，而不是 Minecraft 自建的取消框架。

### 3.3 领域任务状态

现有 Coding Agent 的文件型 `TaskManager` 与编码步骤强耦合，不直接复用。Minecraft 只新增领域状态，不复制通用 Agent Runtime：

- `minecraft_tasks`：任务目标、会话/用户/机器人归属、状态、阶段、基线、当前数量、预算、计划版本、活动动作、错误和时间戳。
- `minecraft_task_events`：低频状态转换、动作摘要、数量变化、恢复和错误。
- `minecraft_task_checkpoints`：裁剪后的世界模型、当前计划 DAG、计划理由、已验证前置条件和重规划触发原因。

使用 PostgreSQL 部分唯一索引保证同一机器人最多一个活动任务；使用版本号或条件更新实现乐观并发控制。创建请求带幂等键，避免网络重试产生重复任务。

任务状态：

```text
pending → connecting → observing → preparing → searching → collecting
       ↘ recovering ────────────────────────────────↗
任意非终态 → succeeded | failed | blocked | cancelled
```

每次状态转换先持久化，再发布 trace。恢复时先连接并重新观察；只有机器人身份、服务器世界标识和目标背包基线可验证时才继续，否则进入 `blocked`。

## 4. 数据与接口契约

### 4.1 有限观察

Bridge 的观察响应只包含：

- 连接状态、协商版本、服务器/世界标识。
- 位置、维度、生命、饥饿、氧气。
- 背包按物品聚合的数量、装备和工具耐久。
- 配置半径内经过数量限制的目标方块与掉落物。
- 当前动作 ID、状态和有限错误信息。
- 邻近危险摘要，不返回完整区块或逐 tick 事件。

Python 使用严格 schema 拒绝未知状态、超长列表、负数量和不受支持的版本。

### 4.2 净新增计数

创建任务时记录 `baseline_count`。每次观察计算：

```text
net_acquired = max(0, current_target_count - baseline_count)
```

只有 `net_acquired >= requested_quantity` 才成功。中途死亡、丢弃或消耗使当前数量下降时，进度同步下降。事件累计值不作为完成依据，避免重复事件和恢复后误计数。

### 4.3 类型化计划与重规划

模型通过现有 `ModelTaskRunner` 生成类型化计划，而不是从固定规则中选择一个函数。输入包括用户目标、裁剪后的世界模型、资源事实、已执行步骤、失败历史和剩余预算。输出使用严格 JSON schema，概念结构如下：

```json
{
  "situation": "当前状态的简短判断",
  "strategy": "选择该资源策略的原因",
  "steps": [
    {
      "goal": "获得可采钻石的工具",
      "action": "collect_blocks",
      "arguments": {"resource": "iron_ore", "count": 3},
      "preconditions": ["has_stone_pickaxe"],
      "success": ["inventory.iron_ore >= 3"],
      "fallback": "reobserve_and_replan"
    }
  ],
  "replan_when": ["path_failed", "new_hazard", "budget_pressure"]
}
```

`plan_validator` 检查每个动作及参数是否属于 Bridge 白名单、依赖是否可能满足、计划是否循环、预算是否足够以及安全约束是否被违反。校验失败会带结构化原因交给模型修订，最多尝试配置次数。

模型不可用、输出持续无效或预算耗尽时，系统只对五类 MVP 资源启用保守的确定性降级计划；降级状态必须在任务报告中标明。该降级用于可用性和安全，不是主智能路径。

计划采用滚动窗口，只详细展开接下来的少量动作；每次执行后根据新观察更新世界模型。Agent 因此能够利用意外发现、绕开重复失败、跳过已经满足的准备步骤，而不是机械执行首次生成的整条链。

## 5. 安全与预算

Python 与 Bridge 双层执行不可绕过的安全规则：

- 禁止攻击玩家、访问容器、使用命令、爆炸物、岩浆和非白名单网络目标。
- 低生命、低饥饿、缺氧、熔岩、坠落或敌对生物风险触发停止或返回安全位置。
- 每个任务限制总时长、动作时长、搜索半径、移动距离、挖掘块数、恢复次数、重规划次数和模型调用数。
- 每个动作再次检查剩余预算；预算耗尽后不再调用 Bridge。
- Bridge URL 在启动时校验为本机或显式允许的受信地址，不接受用户在聊天中提供任意 URL。
- 配置与日志对共享密钥、模型密钥和服务器敏感字段脱敏。

## 6. 配置与部署

新增环境配置：

- `MINECRAFT_AGENT_ENABLED`
- `MINECRAFT_BRIDGE_URL`
- `MINECRAFT_BRIDGE_TOKEN`
- `MINECRAFT_SERVER_HOST`
- `MINECRAFT_SERVER_PORT`
- `MINECRAFT_BOT_USERNAME`
- `MINECRAFT_SERVER_VERSION`（留空时自动协商）
- `MINECRAFT_AUTH_MODE=offline`
- 各类任务、安全和动作预算

Bridge 可作为本地进程运行，并提供可选 Docker Compose `minecraft` profile。不开启功能开关时不注册 Minecraft Agent 与工具，不改变现有服务启动行为。启动健康检查分别验证 PostgreSQL、Bridge 和游戏服务器连接，错误信息区分配置、协议、认证和网络失败。

## 7. 测试策略

### 7.1 Python 单元与集成测试

- 资源指令解析、别名、数量和拒绝场景。
- 资源目录、准备链跳步、工具等级和两种钻石矿。
- 类型化计划 schema、计划校验、循环依赖拒绝和模型修订上限。
- 世界模型能合并发现、记住失败路径并在恢复时裁剪过期事实。
- Critic 在意外资源、重复路径失败、危险和预算压力下选择不同策略。
- 净新增计数、丢失回退和恢复校验。
- 状态机全部合法/非法转换。
- 安全阈值、任务预算、重试上限和终态报告。
- PostgreSQL 幂等创建、单机器人唯一活动任务、乐观并发与重启恢复。
- AgentRouter 路由及现有 bot/coding 回归。
- ToolRegistry 可见性、跨会话拒绝和敏感字段脱敏。
- 共享 `CancellationRegistry` 的当前回合与长期任务取消。
- TraceStore 中的任务/动作关联。

### 7.2 Bridge 测试

- 使用假 Bot Adapter 测试连接、自动版本、观察裁剪和动作生命周期。
- 每个高层动作的 schema、安全守卫、幂等与取消。
- 路径失败、工具损坏、背包满、低生命、断线、死亡和不支持版本。
- Bearer 认证、正文限制、并发限制和非本机绑定保护。

### 7.3 模拟端到端

实现确定性 Fake Bridge 世界，从空背包执行“挖 30 个钻石”：

- 依次准备木制、石制和铁制工具。
- 有限搜索普通与深层钻石矿。
- 模拟掉落、拾取和一次可恢复故障。
- 验证 Agent 根据新发现的现成铁矿动态缩短原计划。
- 验证连续路径失败后生成不同策略，而不是重复调用同一动作。
- 背包净新增达到 30 时成功。
- 验证全过程未调用 shell、任意代码或未注册动作。

### 7.4 真实服务器测试

提供手动 smoke test，仅对专用离线测试服务器执行：

- 自动协商版本并登录。
- 完成地表原木任务。
- 验证状态查询、取消、安全退出和重连。
- 钻石任务允许按预算返回成功或可诊断的 `blocked`。

真实服务器测试默认不进入 CI，不连接用户未明确配置的服务器。

## 8. 计划中的文件变更

预计新增：

- `applications/minecraft/**`
- `plugins/minecraft/**`
- `agents/minecraft.py`
- `minecraft-bridge/**`
- `tests/minecraft/**`
- 数据库迁移或兼容建表模块
- Minecraft 配置示例与运行文档

预计修改：

- `runtime/bootstrap.py`：按功能开关装配共享服务和插件。
- `runtime/routing/**`：增加 Minecraft 意图与 AgentSpec 路由。
- `runtime/agent_loop.py`、`runtime/app_runtime.py`：注入共享取消注册器。
- `tools/**`：登记 `minecraft` 工具模式，不改变已有模式授权。
- trace 事件常量/辅助方法：记录领域事件，不改变现有 trace 格式。
- Docker Compose 与 `.env.example`：增加可选 Bridge 配置。

不会修改：

- Coding Agent 的任务语义和执行分支。
- 普通聊天 Agent 的默认工具权限。
- 历史报告、评测基线和既有会话数据。
- 通用 Runtime 的模型调用与工具循环语义。

## 9. 验收映射

- AC1–AC4：解析器、Bridge DTO、基线计数测试。
- AC5–AC8：资源目录、准备链、动作授权与有界搜索测试。
- AC9–AC12：任务仓储、共享取消、恢复和报告测试。
- AC13：架构测试验证标准 `AgentLoop → Runtime → ToolRegistry/Executor → Plugin` 路径，且不存在 Minecraft 自建模型循环。
- AC14：Python/Bridge 双层安全与认证测试。
- AC15：Fake Bridge 确定性端到端测试。
- AC16：专用真实服务器 smoke test。

## 10. 技术设计完成条件

- 所有功能与非功能需求都有组件、数据或测试承接。
- 长任务取消和恢复边界明确。
- Minecraft 领域代码不复制通用 Runtime。
- Node.js 协议对象不进入 Python Runtime。
- 无未决 `TODO`、`TBD` 或需要实现阶段临时决定的架构问题。
