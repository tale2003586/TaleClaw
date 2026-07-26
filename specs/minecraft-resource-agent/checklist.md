# Minecraft 资源任务 Agent Checklist

> 本清单在实现完成后逐项执行。每一项都必须有命令输出、测试报告或可观察行为作为证据；不能以代码阅读或“应该可用”代替运行验证。

## 1. 验收规则

- [ ] 每个 Phase 只在该 Phase 的全部必选项通过后标记完成。（验证：保存对应测试命令、退出码和失败数）
- [ ] Phase A、B、C 的自动化测试不需要 PostgreSQL、真实 Minecraft Server 或外部网络。（验证：清空相关连接变量并断网运行各 Phase 测试集）
- [ ] 真实服务器 smoke test 始终是显式可选操作，不被 Python/Node 默认测试调用。（验证：搜索测试入口并在无服务器环境运行全测）
- [ ] 所有失败项记录“预期、实际、日志/trace、修复任务、复测结果”。（验证：验收报告不存在无证据的勾选项）

## 2. Phase A：最小垂直闭环

### 2.1 领域合同与 Store

- [ ] `ResourceGoal` 拒绝零、负数、未知资源和多目标，接受“收集 4 个原木”。（验证：`pytest -q tests/minecraft/test_models.py tests/minecraft/test_parser.py`）
- [ ] Domain DTO 可以稳定序列化/反序列化，且不存在 AgentLoop、psycopg 或 Mineflayer 类型泄漏。（验证：`pytest -q tests/minecraft/test_models.py -k serialization`）
- [ ] `MinecraftTaskStore` 是上层依赖的接口，Phase A 使用 `InMemoryMinecraftTaskStore`。（验证：`pytest -q tests/minecraft/test_memory_store.py -k contract`）
- [ ] 重复 idempotency key 返回同一任务，版本冲突不会静默覆盖。（验证：`pytest -q tests/minecraft/test_memory_store.py`）
- [ ] 同一 bot 的第二个活动任务被拒绝，且第一个任务继续保持活动。（验证：`pytest -q tests/minecraft/test_memory_store.py -k single_active_bot`）

### 2.2 计数、状态与报告

- [ ] 资源完成真值只来自动作后的当前背包，不使用累计掉落事件。（验证：`pytest -q tests/minecraft/test_progress.py -k inventory_truth`）
- [ ] 基线为 5、目标净新增 4 时，背包 8 未完成、9 才成功。（验证：`pytest -q tests/minecraft/test_progress.py -k baseline`）
- [ ] 丢弃、死亡丢失或消耗目标物品会降低净新增进度。（验证：`pytest -q tests/minecraft/test_progress.py -k loss`）
- [ ] succeeded、failed、blocked、cancelled 四类报告均包含目标、净新增、耗时和终止原因。（验证：`pytest -q tests/minecraft/test_progress.py -k report`）
- [ ] 任务进入终态后不再提交 Bridge 动作。（验证：`pytest -q tests/minecraft/test_wood_e2e.py -k terminal_stops_actions`）

### 2.3 Bridge MVP

- [ ] Node 依赖可从 lockfile 重复安装，Node 版本要求明确。（验证：`cd minecraft-bridge && npm ci`）
- [ ] Bridge 默认绑定 loopback，使用 offline auth，版本留空时允许 Mineflayer 自动协商。（验证：`cd minecraft-bridge && npm test -- config.test.js bot-adapter.test.js`）
- [ ] 不支持版本、认证失败、服务器拒绝和网络错误在执行游戏动作前被分类。（验证：`cd minecraft-bridge && npm test -- bot-adapter.test.js server.test.js`）
- [ ] BotObservation 只返回位置、维度、生存值、背包、装备、附近原木/掉落/危险和动作状态。（验证：`cd minecraft-bridge && npm test -- bot-adapter.test.js`）
- [ ] Observation 列表、半径和文本有上限，不包含完整区块、逐 tick 原始事件或 Mineflayer 对象。（验证：`cd minecraft-bridge && npm test -- bot-adapter.test.js -t bounded`）
- [ ] `find_blocks` 和 `collect_blocks` 可通过 fake Mineflayer 找到并采集原木。（验证：`cd minecraft-bridge && npm test -- collect-actions.test.js`）
- [ ] 相同动作 idempotency key 不会导致第二次挖掘。（验证：`cd minecraft-bridge && npm test -- action-store.test.js -t idempotency`）
- [ ] 动作取消会触发 AbortSignal、终止当前动作并禁止后续状态回退。（验证：`cd minecraft-bridge && npm test -- action-store.test.js collect-actions.test.js -t cancel`）
- [ ] restricted resource mode 拒绝攻击、容器、命令、爆炸物、岩浆、非白名单破坏、任意代码和原始协议动作。（验证：`cd minecraft-bridge && npm test -- safety.test.js`）

### 2.4 BridgeClient 与 Worker

- [ ] Python `watch_action()` 暴露 AsyncIterator；polling 只是实现细节，可替换 watcher 通过同一 contract。（验证：`pytest -q tests/minecraft/test_bridge_client.py -k watch`）
- [ ] BridgeClient 正确传播超时、错误分类、幂等键和 CancellationToken，日志不含 token。（验证：`pytest -q tests/minecraft/test_bridge_client.py`）
- [ ] Phase A Worker 直接依赖 Store/Bridge/Cancellation ports，不导入 AgentLoop。（验证：`pytest -q tests/minecraft/test_wood_e2e.py -k architecture`）
- [ ] `/minecraft 收集 4 个原木` 的等价直接 Service 请求完成 observe→find→collect→背包验证→succeeded。（验证：`pytest -q tests/minecraft/test_wood_e2e.py`）
- [ ] Phase A 全测在没有 LLM、PostgreSQL和真实服务器时通过。（验证：清空模型与 Minecraft Server 变量后运行 `pytest -q tests/minecraft -k 'phase_a or wood'`）
- [ ] Smoke 脚本默认只检查配置，未传 `--connect` 时没有网络连接。（验证：`python scripts/minecraft_smoke.py --check-only`）

**Phase A 阶段门**

- [ ] Phase A 可独立交付“净新增 4 个原木”闭环。（验证：保存 `test_wood_e2e.py` 完整输出和结构化终态报告）

## 3. Phase B：智能规划与认知门控

### 3.1 世界、Memory 与 Context

- [ ] BotObservation、BeliefWorldState、DomainCatalog 是三个独立类型和职责。（验证：`pytest -q tests/minecraft/test_world_state.py -k boundaries`）
- [ ] 易失世界事实包含 `observed_at`、`expires_at`、`confidence` 和 `source`。（验证：`pytest -q tests/minecraft/test_world_state.py -k freshness`）
- [ ] 新观察能更新旧事实，过期资源位置会降权或删除，失败路径会保留有限次数。（验证：`pytest -q tests/minecraft/test_world_state.py`）
- [ ] 静态配方、工具等级和方块映射只存在 DomainCatalog，不复制到 BeliefWorldState。（验证：`pytest -q tests/minecraft/test_world_state.py -k catalog_separation`）
- [ ] Minecraft Memory Adapter 复用 TaleClaw Memory store/scope，并按目标和预算检索。（验证：`pytest -q tests/minecraft/test_memory_adapter.py`）
- [ ] 普通动作轮次不检索全部通用记忆；只在初始规划、全局重规划、LLMCritic 和重要 checkpoint 检索。（验证：`pytest -q tests/minecraft/test_memory_adapter.py -k retrieval_gate`）
- [ ] Planner/Critic Context Builder 复用 ContextBudgeter/Memory/prompt assets，不注入普通聊天历史。（验证：`pytest -q tests/minecraft/test_context.py`）
- [ ] Planner 与 Critic 上下文均在配置预算内且不含凭证。（验证：`pytest -q tests/minecraft/test_context.py -k bounded_and_redacted`）

### 3.2 Planner 与 Validator

- [ ] TaskPlan 包含 plan version、策略理由、前置条件、成功判据、fallback 和重规划触发事件。（验证：`pytest -q tests/minecraft/test_plan_models.py`）
- [ ] Planner、PlanRevision、LLMCritic 直接使用共享 `ModelTaskRunner`/ModelPool，不经过 AgentLoop。（验证：`pytest -q tests/minecraft/test_model_gateway.py -k direct_runner`）
- [ ] 三类模型任务可以使用不同 model purpose/config，并记录 timeout、usage/token 和 trace 元数据。（验证：`pytest -q tests/minecraft/test_model_gateway.py`）
- [ ] `ModelTaskRunner.run()` 的现有字符串返回行为保持兼容。（验证：`pytest -q tests/test_model_task_runner.py`）
- [ ] Planner 只生成短滚动窗口，可根据已有材料跳过步骤。（验证：`pytest -q tests/minecraft/test_planner.py -k rolling_plan`）
- [ ] Planner 没有 ReasoningGate 批准时不能调用模型。（验证：`pytest -q tests/minecraft/test_planner.py -k requires_gate`）
- [ ] PlanValidator 拒绝未知动作、非法参数、依赖环、错误工具等级、超预算和违反安全 profile 的计划。（验证：`pytest -q tests/minecraft/test_plan_validator.py`）
- [ ] 无效模型输出在修订次数内重试；持续无效或模型不可用时进入带来源标记的 fallback。（验证：`pytest -q tests/minecraft/test_planner.py -k 'revision or fallback'`）

### 3.3 LocalEvaluator

- [ ] LocalEvaluator 完全确定性且没有模型依赖。（验证：`pytest -q tests/minecraft/test_local_evaluator.py -k no_model`）
- [ ] 动作成功但预期世界变化未发生时不会错误标记 step succeeded。（验证：`pytest -q tests/minecraft/test_local_evaluator.py -k missing_effect`）
- [ ] 已知错误且存在本地策略时返回 local_recovery，不升级模型。（验证：`pytest -q tests/minecraft/test_local_evaluator.py -k local_recovery`）
- [ ] 无进展、恢复耗尽和目标完成分别返回 escalate、blocked/failed、task_completed。（验证：`pytest -q tests/minecraft/test_local_evaluator.py`）

### 3.4 ReasoningGate 与 LLMCritic

- [ ] 所有 Planner、PlanRevision 和 LLMCritic 调用都携带 ReasoningGate 批准决定。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py tests/minecraft/test_model_gateway.py -k approval`）
- [ ] 重复 event ID 只产生一次认知决定和一次预算消费。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k dedup`）
- [ ] 高频事件被聚合/防抖，逐 tick 事件永远不触发模型。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k debounce`）
- [ ] 普通模型调用遵守冷却；安全事件绕过模型立即抢占。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k 'cooldown or safety'`）
- [ ] 同类失败计数达到上限才升级，重复失败策略会被排除。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k failure_threshold`）
- [ ] 旧 plan version 的迟到事件不能触发当前计划的模型调用。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k stale_plan`）
- [ ] 模型预算耗尽后只能 fallback、blocked 或 failed，调用计数不再增加。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k exhausted`）
- [ ] LLMCritic 只响应 Gate 的升级决定，不在每个动作或每个里程碑调用。（验证：`pytest -q tests/minecraft/test_llm_critic.py`）
- [ ] 每次认知 trace 可回答原因、触发 event、plan version、预算消耗、上下文大小、输出和 Validator 结果。（验证：`pytest -q tests/minecraft/test_model_gateway.py -k trace_metadata`）

### 3.5 确定性调用决策表

- [ ] 新任务无计划只调用一次 Planner。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k new_task`）
- [ ] 动作正常推进、步骤成功或滚动计划仍有合法步骤时不调用模型。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k normal_progress`）
- [ ] 滚动计划耗尽且目标未完成时调用 Planner 扩展。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k plan_exhausted`）
- [ ] 一次寻路失败、本地有备用工具或背包处理策略时不调用模型。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k local_cases`）
- [ ] 同类寻路失败达到上限、工具前置条件失效或长时间无进展时只升级一次。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k escalation_cases`）
- [ ] 生命、饥饿、氧气、熔岩危险和用户取消不调用模型。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k immediate_interrupts`）
- [ ] 普通新资源只更新世界状态；只有显著改变长期策略时 Gate 才可选批准调用。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k discoveries`）
- [ ] 目标由真实背包确认完成时不调用模型，直接进入完成态。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k completion`）

### 3.6 Worker 与石镐 E2E

- [ ] MinecraftWorker 按观察→世界更新→安全→LocalEvaluator→ReasoningGate→认知/动作→真实验证运行。（验证：`pytest -q tests/minecraft/test_worker_reasoning.py -k order`）
- [ ] SafetyController 能在没有模型时取消当前动作并执行安全策略。（验证：`pytest -q tests/minecraft/test_worker_reasoning.py -k safety_interrupt`）
- [ ] Worker 不重新实现模型池、通用路由、ToolRegistry、Memory store、SessionStore、TraceStore、CancellationRegistry 或 MessageBus。（验证：`pytest -q tests/minecraft/test_worker_reasoning.py -k kernel_boundaries`）
- [ ] “制作一把石镐”会动态完成材料和制作步骤，最终以背包存在石镐为准。（验证：`pytest -q tests/minecraft/test_stone_pickaxe_e2e.py`）
- [ ] 石镐场景中正常步骤不逐步调用模型，本地恢复不调用模型，恢复耗尽只升级一次。（验证：`pytest -q tests/minecraft/test_stone_pickaxe_e2e.py -k call_counts`）

**Phase B 阶段门**

- [ ] Phase B 在 InMemory Store、Fake Bridge 和 fake model 下完整通过。（验证：`pytest -q tests/minecraft -k 'phase_b or stone_pickaxe or reasoning'`）

## 4. Phase C：TaleClaw Kernel 集成

### 4.1 共享实例与取消

- [ ] RuntimeServices 容器共享 ModelPool、ModelTaskRunner、ToolRegistry/Executor、PluginManager 所需对象、Memory、Context、Session、Trace、Cancellation 和 MessageBus。（验证：`pytest -q tests/minecraft/test_kernel_services.py`）
- [ ] `AgentLoop` 不属于 Kernel 服务容器，MinecraftWorker 也不持有 AgentLoop 引用。（验证：`pytest -q tests/minecraft/test_kernel_services.py -k boundaries`）
- [ ] AgentLoop 与 AppRuntime 改用共享 CancellationRegistry 后，现有聊天取消行为不变。（验证：`pytest -q tests/test_agent_loop_phases.py tests/minecraft/test_cancellation.py`）
- [ ] 聊天回合 scope 和 Minecraft task scope 相互隔离，聊天回合结束不会释放长期任务 token。（验证：`pytest -q tests/minecraft/test_cancellation.py -k scopes`）
- [ ] 取消传播顺序为持久/Store 状态→token→Worker→Bridge cancel→停止后续动作。（验证：`pytest -q tests/minecraft/test_cancellation.py -k propagation`）

### 4.2 Session、Trace、Memory、Context 与 MessageBus

- [ ] UserSession 只保存 task ID/摘要关联，不保存完整世界状态或计划历史。（验证：`pytest -q tests/minecraft/test_session_progress.py -k session_boundary`）
- [ ] API/Benchmark 无 MessageBus 时可使用 null publisher；聊天入口通过 OutboundMessage 收到节流进度。（验证：`pytest -q tests/minecraft/test_session_progress.py`）
- [ ] 内部任务事件写入 Store/Trace，不把 MessageBus 当作领域 EventBus。（验证：`pytest -q tests/minecraft/test_session_progress.py -k internal_events`）
- [ ] Minecraft 事件包含 task/run/session/plan/event/action 关联并可由 TraceStore 订阅。（验证：`pytest -q tests/minecraft/test_tracing.py`）
- [ ] Minecraft 使用与 Chat/Coding 相同的 Memory、ContextBudgeter、TraceStore 和 ModelPool 实例。（验证：`pytest -q tests/minecraft/test_kernel_context_memory.py`）

### 4.3 Tool、Plugin 与入口

- [ ] Minecraft tool mode 只暴露 start/status/cancel/bot status 四个外部工具。（验证：`pytest -q tests/minecraft/test_agent_spec.py tests/minecraft/test_plugin.py`）
- [ ] shell、文件写入、子 Agent、任意网络和 Bridge 内部动作不会出现在 Minecraft tool catalog。（验证：`pytest -q tests/minecraft/test_agent_spec.py -k isolation`）
- [ ] 四个 Plugin handler 只调用同一个 MinecraftTaskService，不执行 Worker 循环或直接调用模型。（验证：`pytest -q tests/minecraft/test_plugin.py -k delegation`）
- [ ] Plugin 工具经过现有 ToolRegistry/ToolExecutor 的 schema、session 授权和 trace。（验证：`pytest -q tests/minecraft/test_plugin.py -k runtime_tools`）
- [ ] `/minecraft 收集 4 个原木` 稳定进入显式 Minecraft AgentSpec，普通 Minecraft 问答仍走 bot。（验证：`pytest -q tests/minecraft/test_explicit_routing.py`）
- [ ] `application_mode=minecraft` 与显式命令调用相同的 TaskService。（验证：`pytest -q tests/minecraft/test_explicit_routing.py -k metadata_mode`）
- [ ] 自动意图分类关闭时不影响 MVP；开启时仅高置信单资源命令命中。（验证：`pytest -q tests/minecraft/test_optional_routing.py`）
- [ ] API、CLI、Benchmark 在 AgentLoop 被替换为抛错 stub 时仍可创建、查询和取消任务。（验证：`pytest -q tests/minecraft/test_direct_entrypoints.py`）
- [ ] 聊天、API、CLI、Benchmark 产生同一 CreateRequest schema 和任务状态语义。（验证：`pytest -q tests/minecraft/test_direct_entrypoints.py -k equivalent`）

### 4.4 Runtime 回归与边界

- [ ] Minecraft 功能关闭时不连接 Bridge、不注册工具、不改变 bootstrap 行为。（验证：`pytest -q tests/minecraft/test_runtime_integration.py -k disabled tests/test_config_bootstrap.py`）
- [ ] Minecraft 功能开启时使用共享 Kernel 实例，且 Worker 拥有独立领域循环。（验证：`pytest -q tests/minecraft/test_runtime_integration.py -k enabled`）
- [ ] AgentLoop 创建任务返回后不参与逐动作执行或持续认知。（验证：`pytest -q tests/minecraft/test_runtime_integration.py -k agent_loop_stops_at_service`）
- [ ] Bot、Coding、Hybrid 原有路由和权限无回归。（验证：`pytest -q tests/test_hybrid_mode_routing.py tests/test_mode_switch.py tests/test_tool_safety.py`）
- [ ] Plugin、Session 和 Trace 原有测试通过。（验证：`pytest -q tests/test_plugin_manager.py tests/test_session_store_incremental.py tests/test_run_trace.py`）

**Phase C 阶段门**

- [ ] 三种入口最终调用相同 TaskService，MinecraftWorker 不依赖 AgentLoop。（验证：`pytest -q tests/minecraft/test_runtime_integration.py tests/minecraft/test_direct_entrypoints.py`）

## 5. Phase D：生产化与长期任务

### 5.1 PostgreSQL、并发与恢复

- [ ] PostgreSQL schema 可在空库和已有库上重复初始化，新增 NOT NULL 字段有安全回填。（验证：`pytest -q tests/minecraft/test_postgres_store_schema.py`）
- [ ] Postgres Store 通过与 InMemory Store 相同的 contract tests。（验证：`pytest -q tests/minecraft/test_memory_store.py tests/minecraft/test_postgres_store.py -k contract`）
- [ ] task/run/event/checkpoint 写入保持幂等，重复事件和重复创建不会产生第二记录。（验证：`pytest -q tests/minecraft/test_postgres_store.py -k idempotency`）
- [ ] 乐观并发更新只有一个成功，冲突返回可诊断结果。（验证：`pytest -q tests/minecraft/test_postgres_store.py -k optimistic`）
- [ ] 同 bot 只有一个活动任务；两个 Worker 竞争 lease 时只有一个执行动作。（验证：`pytest -q tests/minecraft/test_worker_lease.py`）
- [ ] lease 过期可安全接管，旧 Worker 不能继续写动作结果。（验证：`pytest -q tests/minecraft/test_worker_lease.py -k takeover`）
- [ ] 重启恢复先重新连接和观察，再校验 bot/server/world/baseline/plan version。（验证：`pytest -q tests/minecraft/test_resume.py`）
- [ ] 身份、世界或基线不一致时任务 blocked，不直接沿用旧成功进度。（验证：`pytest -q tests/minecraft/test_resume.py -k mismatch`）

### 5.2 故障、安全和长期取消

- [ ] 一次路径失败使用本地恢复；达到同类上限才生成一次认知升级。（验证：`pytest -q tests/minecraft/test_recovery.py -k path`）
- [ ] 工具损坏、背包满、低生命、低饥饿、断线和死亡均进入有界恢复或明确阻塞。（验证：`pytest -q tests/minecraft/test_recovery.py`）
- [ ] 恢复次数耗尽后不再产生动作或模型调用。（验证：`pytest -q tests/minecraft/test_recovery.py -k exhausted`）
- [ ] 用户取消先记录取消状态，再取消 token 和 Bridge 当前动作。（验证：`pytest -q tests/minecraft/test_long_task_cancellation.py -k ordering`）
- [ ] 用户取消或进程恢复读到取消状态后，不产生任何后续动作。（验证：`pytest -q tests/minecraft/test_long_task_cancellation.py`）
- [ ] 动作、时间、搜索、挖掘、恢复和模型预算耗尽后均停止对应活动。（验证：`pytest -q tests/minecraft/test_reasoning_budget.py tests/minecraft/test_recovery.py -k budget`）

### 5.3 五资源与地下动作

- [ ] 原木、圆石、煤、铁原矿和钻石均有别名、掉落、依赖、工具等级和采集策略。（验证：`pytest -q tests/minecraft/test_catalog.py`）
- [ ] 普通钻石矿与深层钻石矿均映射到钻石资源。（验证：`pytest -q tests/minecraft/test_catalog.py -k diamond_ores`）
- [ ] 木镐/石镐挖钻石在 Python Validator 和 Node Safety 两层都被拒绝。（验证：`pytest -q tests/minecraft/test_plan_validator.py -k diamond_tool && (cd minecraft-bridge && npm test -- collect-actions.test.js -t diamond)`）
- [ ] 铁镐或更高级镐能采集两类钻石矿。（验证：`cd minecraft-bridge && npm test -- collect-actions.test.js -t diamond`)
- [ ] craft、equip、eat、return_safe 动作具有 schema、安全检查、超时和取消。（验证：`cd minecraft-bridge && npm test -- basic-actions.test.js`）
- [ ] branch_mine 受隧道形状、距离、挖掘数、生存值和危险检查限制。（验证：`cd minecraft-bridge && npm test -- branch-mine.test.js`）
- [ ] branch_mine 遇到目标、危险、预算耗尽或取消时立即停止并返回结构化结果。（验证：`cd minecraft-bridge && npm test -- branch-mine.test.js -t terminal`）

### 5.4 认证、部署和可替换监控

- [ ] Bridge Bearer 认证、远程 allowlist、请求大小和并发限制有效。（验证：`cd minecraft-bridge && npm test -- server-auth.test.js`）
- [ ] 服务器地址和 Bot 身份只来自可信配置，聊天/API payload 不能覆盖。（验证：`pytest -q tests/minecraft/test_bridge_client.py -k trusted_config && (cd minecraft-bridge && npm test -- server-auth.test.js -t override)`）
- [ ] token、模型密钥、服务器凭证不出现在日志、Trace、task state、fixture 或文档。（验证：运行项目凭证扫描并人工检查命中项）
- [ ] polling watcher 与 fake SSE watcher 通过同一 contract，Worker 不感知传输方式。（验证：`pytest -q tests/minecraft/test_action_watch_contract.py`）
- [ ] 默认 Compose 不启动 Minecraft，`minecraft` profile 配置合法。（验证：`docker compose config && docker compose --profile minecraft config`）
- [ ] `.env.example` 使用安全默认值且不含可用真实密钥。（验证：`pytest -q tests/minecraft/test_config_docs.py`）

### 5.5 30 钻石长程 E2E

- [ ] 空背包任务会准备木材、工作台、石镐和铁镐，再采集钻石。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k prerequisite_chain`）
- [ ] 已有合格工具或材料时动态跳过无关准备步骤。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k skip_satisfied`）
- [ ] Fake 世界的新发现能改变长期策略，而不是机械执行旧计划。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k dynamic_replan`）
- [ ] 无可见资源时使用有界搜索；预算耗尽后 blocked 且不再产生动作。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k bounded_search`）
- [ ] 起始已有 5 个钻石、目标新增 30 时，34 未完成、35 succeeded。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k baseline_35`）
- [ ] 中途故障、checkpoint 和恢复不会重复动作或重复计数。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k recovery_idempotency`）
- [ ] 最终报告显示净新增 30、计划变化、主要准备阶段、耗时和背包摘要。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k final_report`）

**Phase D 阶段门**

- [ ] PostgreSQL 恢复、长期取消、五资源和 30 钻石 E2E 全部通过。（验证：`pytest -q tests/minecraft`）

## 6. Spec 验收标准映射

- [ ] AC1：资源解析正反例全部通过。（验证：`pytest -q tests/minecraft/test_parser.py`）
- [ ] AC2：离线配置、版本协商、连接事件和失败分类通过。（验证：`cd minecraft-bridge && npm test -- config.test.js bot-adapter.test.js`）
- [ ] AC3：有限观察和无逐 tick/无界数据通过。（验证：`cd minecraft-bridge && npm test -- bot-adapter.test.js -t bounded`）
- [ ] AC4：净新增 30 的基线与损失回退通过。（验证：`pytest -q tests/minecraft/test_progress.py tests/minecraft/test_diamond_e2e.py -k baseline`）
- [ ] AC5：钻石准备链、动态跳步和计划校验通过。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k prerequisite`）
- [ ] AC6：外部工具、内部动作和任意代码/协议隔离通过。（验证：`pytest -q tests/minecraft/test_agent_spec.py tests/minecraft/test_plan_validator.py && (cd minecraft-bridge && npm test -- schemas.test.js safety.test.js)`）
- [ ] AC7：五资源、工具门槛和两类钻石矿通过。（验证：`pytest -q tests/minecraft/test_catalog.py && (cd minecraft-bridge && npm test -- collect-actions.test.js)`）
- [ ] AC8：有界搜索和预算终止通过。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py -k bounded_search`）
- [ ] AC9：状态查询、取消和单 bot 单任务通过。（验证：`pytest -q tests/minecraft/test_long_task_cancellation.py tests/minecraft/test_worker_lease.py`）
- [ ] AC10：路径、工具、背包、生存、断线和死亡恢复通过。（验证：`pytest -q tests/minecraft/test_recovery.py`）
- [ ] AC11：持久恢复和不一致阻塞通过。（验证：`pytest -q tests/minecraft/test_resume.py`）
- [ ] AC12：四类结构化终态报告通过。（验证：`pytest -q tests/minecraft/test_progress.py -k report`）
- [ ] AC13：Kernel 复用、领域 Worker 和三入口边界通过。（验证：`pytest -q tests/minecraft/test_runtime_integration.py tests/minecraft/test_direct_entrypoints.py`）
- [ ] AC14：双层安全、认证、预算和凭证保护通过。（验证：`pytest -q tests/minecraft -k 'safety or budget or credential' && (cd minecraft-bridge && npm test -- safety.test.js server-auth.test.js)`）
- [ ] AC15：确定性模拟“净新增 30 个钻石”通过。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py`）
- [ ] AC16：专用离线服务器 smoke 成功或返回可诊断 blocked。（验证：用户显式提供服务器后运行 `python scripts/minecraft_smoke.py --connect`）

## 7. 架构自检

- [ ] MinecraftWorker 不导入、不持有、不调用 AgentLoop。（验证：架构测试加 `rg -n 'AgentLoop' applications/minecraft` 无生产代码命中）
- [ ] Planner/LLMCritic 不通过 AgentLoop 调用模型。（验证：`pytest -q tests/minecraft/test_model_gateway.py -k direct_runner`）
- [ ] Worker、Critic、RecoveryController、StateMachine 和事件处理器无法绕过 ReasoningGate 调用模型。（验证：`pytest -q tests/minecraft/test_reasoning_gate.py -k all_callers`）
- [ ] 普通动作完成和普通里程碑不触发模型。（验证：`pytest -q tests/minecraft/test_stone_pickaxe_e2e.py -k call_counts`）
- [ ] SafetyController 无模型也能立即抢占。（验证：`pytest -q tests/minecraft/test_worker_reasoning.py -k safety_interrupt`）
- [ ] LocalEvaluator 完全确定性。（验证：`pytest -q tests/minecraft/test_local_evaluator.py -k no_model`）
- [ ] LLMCritic 只处理升级事件。（验证：`pytest -q tests/minecraft/test_llm_critic.py`）
- [ ] Worker 复用共享模型、Memory、Context、Trace 和 Cancellation 实例。（验证：`pytest -q tests/minecraft/test_runtime_integration.py -k shared_kernel`）
- [ ] API、CLI 和 Benchmark 完全绕过 AgentLoop。（验证：`pytest -q tests/minecraft/test_direct_entrypoints.py`）
- [ ] AgentLoop 只负责聊天入口的创建、查询、取消和状态返回。（验证：`pytest -q tests/minecraft/test_runtime_integration.py -k agent_loop_boundary`）
- [ ] Phase A 在没有 PostgreSQL 和 LLM 时运行。（验证：Phase A 阶段门）
- [ ] 所有自动化测试在没有真实 Minecraft Server 时运行。（验证：全测期间监听网络调用并拒绝外部连接）
- [ ] Bridge 无任意代码和原始协议动作入口。（验证：`cd minecraft-bridge && npm test -- schemas.test.js safety.test.js`）
- [ ] 当前背包是资源任务完成的唯一真值来源。（验证：`pytest -q tests/minecraft/test_progress.py -k inventory_truth`）
- [ ] Observation、BeliefWorldState 和 DomainCatalog 保持边界。（验证：`pytest -q tests/minecraft/test_world_state.py -k boundaries`）

## 8. 编译、全量测试与静态检查

- [ ] Minecraft Python 模块编译通过。（验证：`python -m compileall applications/minecraft plugins/minecraft agents/minecraft.py runtime/cancellation.py`）
- [ ] Minecraft Python 测试全部通过。（验证：`pytest -q tests/minecraft`）
- [ ] Bridge 从 lockfile 安装并全测通过，结束后没有残留 server、bot 或 timer。（验证：`cd minecraft-bridge && npm ci && npm test`）
- [ ] TaleClaw 全量 Python 测试通过。（验证：`pytest -q`）
- [ ] Docker Compose 默认及 minecraft profile 均可解析。（验证：`docker compose config && docker compose --profile minecraft config`）
- [ ] 没有 `TODO`、`TBD`、跳过测试或未解释的 xfail 留在 Minecraft 生产代码和测试。（验证：`rg -n 'TODO|TBD|pytest\\.skip|xfail|\\.skip\\(' applications/minecraft plugins/minecraft minecraft-bridge tests/minecraft`）
- [ ] 没有任意代码执行、shell、原始协议包或未授权 URL 的生产路径。（验证：运行安全测试并审查安全扫描命中）
- [ ] 所有后台 Worker、HTTP client、Bridge server 和模型超时执行器在测试结束后正常关闭。（验证：全测无 hanging、资源警告或残留进程）

## 9. 最终端到端场景

- [ ] 场景 1：直接 Service 输入“收集 4 个原木”→ observe→find→collect→净新增 4→succeeded。（验证：`pytest -q tests/minecraft/test_wood_e2e.py`）
- [ ] 场景 2：输入“制作一把石镐”→ Planner 生成多步滚动计划→本地恢复不调用模型→背包出现石镐。（验证：`pytest -q tests/minecraft/test_stone_pickaxe_e2e.py`）
- [ ] 场景 3：聊天 `/minecraft 收集 4 个原木`、API 和 Benchmark 创建同一种任务，聊天回合返回后 Worker 独立继续。（验证：`pytest -q tests/minecraft/test_runtime_integration.py tests/minecraft/test_direct_entrypoints.py`）
- [ ] 场景 4：运行中取消→状态先持久化→当前动作取消→无后续动作→cancelled 报告。（验证：`pytest -q tests/minecraft/test_long_task_cancellation.py`）
- [ ] 场景 5：进程重启→重新观察→身份一致时恢复、身份不一致时 blocked。（验证：`pytest -q tests/minecraft/test_resume.py`）
- [ ] 场景 6：空背包“挖 30 个钻石”→动态准备/搜索/恢复→当前背包净新增 30→succeeded。（验证：`pytest -q tests/minecraft/test_diamond_e2e.py`）
- [ ] 场景 7（可选真实服务器）：显式连接专用离线服务器→自动协商版本→采集地表原木→安全退出；失败时返回明确诊断。（验证：`python scripts/minecraft_smoke.py --connect`）

## 10. 完成判定

- [ ] AC1–AC15 全部有自动化通过证据；AC16 在有专用服务器时执行，否则明确标记“可选、未执行”，不能伪造通过。（验证：逐项核对第 6 节证据链接或命令输出）
- [ ] Phase A–D 阶段门全部通过。（验证：核对四个阶段门均有成功测试记录）
- [ ] 架构自检 15 项全部为“是”。（验证：执行第 7 节全部验证命令并记录结果）
- [ ] 全量 Python/Node/Compose 验证通过。（验证：执行第 8 节全部命令且退出码为 0）
- [ ] 没有高优先级安全问题、凭证泄漏、无限重试、模型绕过 ReasoningGate 或 Worker→AgentLoop 依赖。（验证：汇总安全、预算、架构和凭证扫描结果）
- [ ] 最终验收报告记录实际命令、退出码、通过数、失败修复和可选真实服务器结果。（验证：人工审核验收报告字段完整且可追溯）
