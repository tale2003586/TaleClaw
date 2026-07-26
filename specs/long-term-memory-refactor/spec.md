# TaleClaw 长期记忆系统渐进式重构 Spec

## 背景

TaleClaw 当前已经具备 Session 消息持久化、Working Memory、用户级 Markdown 记忆、候选晋升、Coding Task-local Memory、PostgreSQL 历史归档和 Qdrant 语义检索，但这些能力的领域边界和事实来源不清晰。

基于当前代码审计，真实问题包括：

- Session 原始 turn、`HISTORY.md`、`RECENT_CONTEXT.*` 和 Qdrant 历史向量重复保存相近信息。
- `MEMORY.md`、`SELF.md`、`NOW.md`、`PENDING.md` 和 `HISTORY.md` 会被整份重复索引，Qdrant 内容可能落后于文件状态。
- `PENDING.md` 与 `PENDING.json` 同时承担候选职责，实际数据已经明显漂移。
- 有查询文本时，Markdown recall 会混合稳定记忆、候选和历史流水，使过去对话被误当作当前事实。
- 普通候选晋升与 Coding Conclusion 使用两套不同的数据模型和写入链路。
- `memorize` 工具可以直接追加 Markdown，绕过作用域、来源、冲突、版本和撤销治理。
- PostgreSQL 只承载历史归档；长期语义记忆仍以 Markdown 为权威来源。
- 普通 Session 按用户级 scope 检索 Qdrant，因此新 Session 会召回其他 Session 的原始 turn。
- 当前仓库不存在 Minecraft Application；本次只建立 Application State 边界，不编写虚构适配代码。

本次采用渐进迁移，不一次性删除旧链路。每个阶段必须可运行、可测试、可回滚。

## 目标

- PostgreSQL 成为长期语义记忆的唯一权威事实来源。
- Qdrant 成为可从 PostgreSQL 或 Session Store 重建的派生检索索引。
- 明确分离 Session History、Episodic History、Long-term Semantic Memory、Working Memory 和 Application-local State。
- 普通 Session 只能检索本 Session 的原始历史；跨 Session 只读取有效长期语义记忆。
- 统一显式记忆、自动候选和 Coding Conclusion 的写入与晋升治理。
- 支持作用域、类型、状态、来源、证据、版本、替代、撤销、过期和遗忘。
- Markdown 降级为只读导出或兼容视图，不再作为业务真源。
- 在不中断聊天和 Coding Application 的前提下迁移旧数据并退役重复存储。

## 状态边界

### Session History

- 保存某个 Session 中真实发生的消息和工具交互，权威来源为 Session Store。
- 普通对话的语义召回必须限制在当前 Session。
- Session History 不是长期事实，不能直接作为当前有效记忆注入其他 Session。

### Episodic History

- 表示某个 Session 或 Task 过去发生的相关事件。
- 在上下文中必须明确标注为历史事件。
- 普通聊天默认只允许当前 Session；Coding 按当前 Task、Workspace 或 Project 的可信边界检索。

### Long-term Semantic Memory

- 只保存未来仍可能有用的稳定偏好、事实、决策、流程、约束和关系。
- 权威来源为 PostgreSQL。
- 只有当前有效版本可以进入上下文。
- 用户级记忆可以跨 Session；项目、工作区、应用和任务记忆必须匹配其作用域。

### Working Memory

- 保存当前目标、进度、证据、失败尝试、待办和下一步。
- 继续复用现有 Working Memory 与 Coding Context State，不另建重复系统。
- 不自动成为长期语义记忆。

### Application-local State

- 由 Coding 等垂直 Application 管理自己的领域状态。
- 只有经过显式 proposal 与 promotion 后才能进入长期记忆。
- 不合并进全局 Memory 表。

## 功能需求

### 领域模型与事实来源

- F1: 每条长期记忆必须具有稳定 ID、所有者作用域和 ID、受控类型、内容、状态、置信度、显著性、来源、证据、有效时间、版本、替代关系和审计时间。
- F2: 所有者作用域至少支持用户、项目、应用、工作区和任务；项目事实不得写成用户全局事实。
- F3: 类型至少支持偏好、事实、决策、流程、约束和关系，禁止任意字符串扩散。
- F4: 状态至少支持候选、有效、已替代、已撤销、已过期和已拒绝；只有有效且未过期的当前版本可召回。
- F5: PostgreSQL 保存长期记忆、证据、来源、版本和状态，并成为唯一事实来源。
- F6: Qdrant 只保存检索索引和必要元数据；命中后必须回源 PostgreSQL 验证状态与版本。

### 统一写入与生命周期

- F7: 显式记忆、自动候选、Coding Conclusion、更新、替代、撤销和遗忘必须经过统一应用服务。
- F8: 用户明确要求记住的内容可以直接有效，但必须完成可信作用域、类型、重复和冲突检查、来源记录、持久化、索引调度和 Trace。
- F9: 系统推断只能先成为候选；单次出现或同一任务内重复不能直接晋升。
- F10: 候选晋升必须考虑独立 Session 证据、否定或修正、作用域、未来价值、置信度和用户确认要求。
- F11: Coding Conclusion 必须转换为统一 proposal，并记录 workspace、repository、代码版本、task 和证据位置；未经验证不能直接有效。
- F12: 相同作用域、类型和规范化内容的精确重复不得创建第二条有效记忆。
- F13: 语义重复应合并证据或确认时间，不能重复注入上下文。
- F14: 冲突的新事实应创建新版本并替代旧版本；不同作用域不得误判为冲突。
- F15: 遗忘请求必须撤销匹配记忆、停止索引和上下文注入，同时保留最小审计记录。

### 检索与上下文

- F16: 长期语义检索与历史事件检索必须使用独立接口和结果类型。
- F17: 语义检索必须过滤作用域、有效状态、有效时间和当前版本，并综合相关性、置信度、显著性与新鲜度排序。
- F18: 历史检索必须在向量过滤阶段约束 Session、Task、Workspace 或 Project，不能先召回越界数据再裁剪。
- F19: 普通 Session 只能读取当前 Session 的历史；新 Session 不得读取同一用户其他 Session 的原始对话。
- F20: Context Builder 必须把语义记忆、历史事件和 Working Memory 分区呈现。
- F21: 已撤销、已替代、已过期、已拒绝和作用域不匹配的记忆不得进入上下文。
- F22: 同一记忆一次最多注入一次，并受独立 token 预算控制。

### 索引一致性

- F23: 创建、更新、替代、撤销和过期必须产生可重试的索引同步事件。
- F24: PostgreSQL 提交不依赖 Qdrant 成功；索引失败必须可观察、可重试且不回滚事实。
- F25: 支持从 PostgreSQL 安全重建语义记忆索引，并移除旧版本与无效状态索引。
- F26: Session Event 以单个事件或 turn 为索引粒度；禁止反复索引整份记忆或历史文件。

### Markdown 与迁移

- F27: Markdown 只能作为自动生成的只读导出或过渡兼容视图，新的长期记忆不得直接追加 Markdown。
- F28: 旧数据导入必须幂等、支持 dry-run、去重、迁移报告和失败恢复，且不得自动删除源文件。
- F29: 旧 `MEMORY.md` 稳定条目可导入语义记忆，`PENDING.json` 可导入候选；`HISTORY.md` 与 `RECENT_CONTEXT.*` 不得导入为 active。
- F30: `SELF.md`、`NOW.md`、`PENDING.md` 和历史归档必须按明确规则分类为导入、跳过、兼容导出或人工复核。
- F31: 迁移期只允许 PostgreSQL 主写、旧链路只读或单向导出，禁止双向同步。

### 可观测性与评测

- F32: 候选创建、晋升、确认、拒绝、更新、替代、撤销、过期、索引、检索和上下文丢弃必须写入结构化 Trace。
- F33: Trace 至少包含记忆 ID、作用域、类型、版本、来源、原因、置信度、分数、前版本 ID 和索引状态，不记录不必要的敏感全文。
- F34: 评测提供写入数、候选数、晋升率、拒绝率、替代率、撤销率、重复率、冲突率、检索命中率、无效检索率、陈旧命中率、跨作用域泄漏率、索引失败率和上下文 token 占比。

## 非功能需求

- N1: 分阶段迁移，每阶段可以独立部署、测试和回滚。
- N2: 领域层不依赖 PostgreSQL、Qdrant、Markdown、具体 Application 或模型供应商。
- N3: 状态变更使用 PostgreSQL 事务和并发控制，避免并发更新同时有效。
- N4: PostgreSQL 与 Qdrant 最终一致；远程索引调用不得发生在数据库事务内。
- N5: 作用域来自服务端可信 Session、用户、Workspace、Project、Application 和 Task 上下文。
- N6: Repository 不向上层泄露数据库驱动或 SQL 行对象。
- N7: Qdrant 不可用时写入仍可提交；检索降级不得放宽状态或作用域过滤。
- N8: 新公共接口具有类型注解、文档、明确错误分类和结构化日志。
- N9: 没有基线测试与迁移报告时不删除旧文件、表或索引。
- N10: 普通聊天、Session 恢复、Coding、Working Memory、工具安全和 Security RAG 不得回归。
- N11: 数据库升级遵循当前幂等初始化模式，并提供可审计、可重复执行的升级与等价回滚步骤。

## 渐进迁移阶段

- P0 审计与基线：真实数据流报告、保护性测试和旧行为基线。
- P1 领域与存储：领域模型、PostgreSQL Repository、证据、来源和索引 outbox，不切换读取。
- P2 显式记忆垂直切片：remember、更新、撤销、遗忘、索引、语义检索和 Context 接入。
- P3 候选与 Coding：统一自动候选与 Coding Conclusion，替换 Markdown Pending 主链路。
- P4 检索分离：Semantic Memory 与 Episodic History 分离，普通历史限制在当前 Session。
- P5 数据迁移与索引重建：dry-run、导入 PostgreSQL、重建 Qdrant、验证一致性。
- P6 旧实现退役：Markdown 纯导出，停止整文件索引和重复历史文件写入。

## 不做的事

- 不引入知识图谱数据库或完全自治的自进化记忆。
- 不允许模型任意创建、更新或删除所有记忆。
- 不用模型处理所有去重、状态、作用域和版本规则。
- 不重写 Working Memory 或 Coding Context State。
- 不把所有 Session 消息提升为长期记忆。
- 不让 Qdrant 或 Markdown 成为事实来源。
- 不把 Application State 合并进全局长期记忆。
- 不为当前不存在的 Minecraft Application 创建空适配代码。
- 不在单一阶段完成所有旧数据清理和接口删除。
- 不物理删除撤销记忆，除非未来另有合规需求和审批。

## 交付物

- `docs/refactors/memory-system-audit.md`
- `docs/architecture/MEMORY_ARCHITECTURE.md`
- `docs/migrations/long-term-memory-migration.md`
- 可运行的显式记忆垂直切片及后续渐进迁移代码
- 领域、Repository、索引、Context、Coding 和兼容性测试
- `memory-refactor-summary.md`

## 验收标准

- AC1: 显式记忆创建用户级 active preference，写入 PostgreSQL、产生索引事件并可跨 Session 检索；Markdown 不是前提。
- AC2: 偏好更新创建新版本并替代旧版本，只有新版本进入上下文。
- AC3: 遗忘后记忆变为 revoked，Qdrant 与 Context 不再返回有效命中。
- AC4: 单次推断只能成为 candidate，不能直接 active。
- AC5: 项目 A 的事实绑定正确 Workspace 或 Project，不得进入项目 B。
- AC6: Coding Conclusion 进入统一 proposal，Task-local 状态和未验证结论不能污染用户全局 active memory。
- AC7: 新普通 Session 不命中其他 Session 原始 turn，但能命中同一用户 active semantic memory。
- AC8: 当前 Session 的旧 turn 可作为 episodic history 检索，并明确标记为过去事件。
- AC9: superseded、revoked、expired、rejected 和越界记忆不进入 Context。
- AC10: Qdrant 缺失或陈旧时，PostgreSQL 回源过滤无效版本；重建命令可恢复一致性。
- AC11: importer dry-run 零写入，重复执行不重复导入，并报告导入、跳过、冲突和失败。
- AC12: 旧 `MEMORY.md` 有效条目迁移后可检索；历史、近期上下文和未确认 Pending 不会误导入 active。
- AC13: Trace 区分语义记忆、历史事件、候选晋升和索引同步，并能计算泄漏与陈旧命中指标。
- AC14: PostgreSQL 可用而 Qdrant 不可用时，写入成功并进入待重试状态，聊天不被阻断。
- AC15: Session、Working Memory、Coding、Context Builder 和现有相关测试在各阶段保持通过。
