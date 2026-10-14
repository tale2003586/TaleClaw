# 相似项目调研：模型配置与记忆治理

调研日期：2026-10-13。范围：Open WebUI、LiteLLM、Letta、Mem0 的官方文档或官方仓库文档；不调用真实模型、不测试用户凭证。以下“官方事实”和“本项目建议”分开记录。网页与 `main` 分支会变化，本文是当日核验记录，不能作为固定版本的兼容性承诺。

## 1. Open WebUI：连接探测与配置来源

**官方事实**：管理员可维护 API 连接；Verify Connection 调用 `/models`，使用 Bearer 鉴权，并提供模型 ID 提示。官方明确指出，有些供应商没有该端点或采用其他鉴权，探测失败仍可手填模型 ID；保存连接本身不会执行探测。[连接文档](https://docs.openwebui.com/getting-started/quick-start/connect-a-provider/starting-with-openai-compatible/)

**可复用设计**：设置页提供“探测连接 / 获取模型”，发现结果辅助填写 model，同时保留手动输入。成功文案应说明“模型列表端点可访问”，避免用户误以为已确认指定模型的生成能力。

**官方事实**：`ConfigVar` 首次由环境初始化，之后使用持久化值；管理界面可更新这些值。禁用 `ENABLE_PERSISTENT_CONFIG` 会改为环境来源，且界面修改不持久化。[环境配置文档](https://docs.openwebui.com/reference/env-configuration/)

**本项目适配**：现有 `RuntimeSettingsStore` 已采用“环境初始值 → 完整持久化快照”。应在页面明确当前来源、revision、生效范围，避免用户改 `.env` 后误认为会覆盖网页设置。不要为每个字段再引入隐含的优先级。

## 2. LiteLLM：运行时模型管理与可解释路由

**官方事实**：启用数据库模型存储后，模型增删改和凭证轮换可由 UI / 管理 API 完成，无需重启；列表区分数据库模型与配置文件模型，后者不能在 UI 修改。它也提供已存凭证的连接测试、命名凭证复用和脱敏模型信息接口。[模型管理文档](https://docs.litellm.ai/docs/proxy/model_management)

**可复用设计**：把配置来源与可编辑性展示出来；先校验草稿，随后原子保存，以成功写入的新 revision 为新任务边界。本项目已有快照和 revision，不必引入独立网关才能获得这些体验。

**官方事实**：多实例配置通过数据库轮询同步，默认间隔为 30 秒；该轮询间隔本身在启动时读取。[生产实践文档](https://docs.litellm.ai/docs/proxy/prod) 路由把同一模型组内重试与不同模型组间 fallback 区分处理。[请求流程文档](https://docs.litellm.ai/docs/proxy/architecture)

**本项目建议**：增加本地“有效路由预览”，显示目的路由、默认路由、profile fallback 展开后的实际顺序。直接复用 `ModelPool.route_profile_names()` 的语义，不在前端另写一套算法。显示这是配置计划；动态冷却、实际失败与最终命中仍由执行记录说明。若以后部署多节点，必须定义共享存储及版本收敛，当前本地文件热更新不能自动等同于跨节点实时同步。

## 3. Letta：记忆作用域与并发边界

**官方事实**：Letta V1 用结构化 memory block 组织常驻上下文，可共享、设置只读、查看被哪些 agent 使用；detach 从 agent 上下文移除关联但不删除 block。直接修改 block value 会整体替换，并发写入是最后写入覆盖。[Memory blocks 文档](https://docs.letta.com/v1-sdk/memory/memory-blocks)

**可复用设计**：记忆页面同时说明“归属 / 作用域”和“当前是否可召回”，区分历史记录与正在使用的内容。人工操作需携带用户看到的版本，冲突后刷新，而非静默覆盖。

**本项目适配**：本项目采用检索式结构化记忆，已有 owner scope、状态、validity、evidence 和 version，不应把它改回固定几块文件或全量常驻上下文。优先复用 `MemoryContext` 的受权作用域及 `MemoryTransition.expected_version`。Letta V1 示例已归于 legacy SDK；最新 API 中部分只读字段也标为 deprecated，应借鉴产品边界，不直接移植旧接口。[当前 API 示例](https://docs.letta.com/api/resources/agents/subresources/blocks/methods/update)

## 4. Mem0：纠正、生命周期和历史

**官方事实**：Mem0 支持更新记忆内容、metadata、到期日期；更新内容时同步索引。官方文档区分 Platform 与 OSS 的批量能力和查看方式。[更新文档](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/memory-operations/update.mdx) OSS REST API 包括按 ID 查询、更新、删除与 history，并可按 user / agent / run 过滤。[REST 文档](https://github.com/mem0ai/mem0/blob/main/docs/open-source/features/rest-api.mdx)

**可复用设计**：人工确认候选、拒绝错误候选、撤销已生效记忆，并能看到来源与版本。内容纠正应有历史关联，操作后确保召回索引一致；有筛选 / 分页比固定文件列表更适合增长中的记忆集合。

**本项目适配**：`MemoryCommandService` 已有 confirm / reject / update / revoke；update 创建替代记录并保留关联，因此网页应接入这些领域行为。HTTP 必须约束当前用户及已验证会话作用域，不能直接接受浏览器传来的任意 owner。撤销后的搜索可见性要通过索引边界验证。批量硬删除、自动历史清理和“撤销后任意恢复”为后续独立设计，不应绕过现有状态机。

## 本轮建议选型与验收

调研时基线已有模型快照热更新、密钥不回显、revision 冲突检查、目的路由与 fallback；记忆页已改为数据库投影和分页，但治理按钮尚未接入。推荐在该基线上实现以下增量，不引入这些项目的整套运行时或数据库结构。

| 优先级 | 功能 | 验收重点 |
| --- | --- | --- |
| P0 | Provider 探测与模型发现 | 管理员权限；有限超时与响应大小；无生成调用；不回显上游错误/密钥；不支持 `/models` 时可手填；结果过期时不显示为当前草稿有效。 |
| P0 | 有效路由预览 | 默认/别名/目的路由与 profile fallback 顺序吻合真实 ModelPool；无密钥输出；无需外部调用；草稿错误精确定位。 |
| P0 | 全授权作用域的记忆搜索与状态筛选 | 筛选由数据库在分页前执行；总数与列表采用同一作用域和筛选条件；搜索不局限于已加载的一页；非法状态拒绝。 |
| P0 | 记忆确认 / 拒绝 / 撤销 | 只允许授权作用域；expected_version 冲突可识别；状态机不被绕过；动作有来源；索引召回与状态一致。 |
| P1 | 脱敏配置导出、导入预览 | 导出只含配置结构、凭证存在提示；导入先校验/展示变更，并由当前revision约束保存；未知schema拒绝；明确留空密钥保留规则。 |
| P1 | 记忆纠正与历史关联 | 复用领域 update 与 supersedes 链；来源可见；到期解释清楚；验证原记录不再召回而替代记录可召回。 |

表中的导入预览、安全约束与优先级是本项目设计建议，并非声称其他项目全部提供相同功能。

连接探测新增的边界：草稿改变目标地址时，不应自动把已存密钥发给新目标；禁止携带凭证跟随重定向；本地模型服务是合法需求，应通过明确的部署策略允许，而非把任意浏览器提供的 URL 当作可信地址。Provider 返回的模型名称与错误文本也属于不可信输入，需限制长度、数量和内容，不能变成 HTML 或日志中的凭证。

## 对抗迭代方法

每轮实现后，让独立审查 agent 只做破坏性场景分析与隔离测试：陈旧revision / version、跨用户会话、篡改owner、删除被路由引用的profile、fallback重复或环、修改探测目标、重定向、超大JSON、供应商返回敏感异常、记忆状态与索引不一致。实现 agent 修复有证据的问题，审查 agent 再复验。离线fake transport及临时数据库足以验证这些边界；无需真实API Key或付费模型调用。此流程是本轮协作方式，不等同于产品内已有的后台常驻agent功能。
