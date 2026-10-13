# 03 — 跨任务、跨会话记忆审查与设计建议

> 审查日期：2026-09-27。对象：当前工作区 taleclaw。本文只进行代码审查、公开资料调研、设计和文档编写；没有实施业务改造、安装依赖、启动数据库或向量服务、运行训练、调用模型实验、提交或推送代码。
>
> **结论：项目真正实现了可选的结构化长期记忆，但“有存储和检索”尚未形成可信、可纠错、可删除的完整产品闭环。优先修补现有链路，不建议重写记忆框架或新增向量数据库。**

## 1. 审查基线、范围与证据规则

### 1.1 版本与工作区

| 项目 | 本轮记录 |
|---|---|
| 项目根目录 | `/home/tale/kaggle/mytry` |
| 分支 | `feat/memory-runtime-evolution` |
| HEAD | `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2` |
| 提交时间、说明 | `2026-08-18 09:25:54 +0800`；`harden runtime handler and embedding sharing` |
| 开始时已跟踪文件改动 | `git diff --stat`、`git diff --cached --stat` 均为空 |
| 开始时未跟踪内容 | `docs/system-design (2)/`、`os`；保留原状，不作为当前运行代码证据 |
| 本任务输出 | 仅 `docs/agent-design-review/03-memory.md`；首次检查同名文件和父输出目录均不存在 |
| 审查对象 | HEAD 对应的当前源码、已跟踪项目说明和测试；不读取真实 `.env`、用户会话正文、数据库数据目录及现有私密记忆 |
| 项目规范 | 检查根目录和父目录链，并在源码/文档与隐藏配置目录搜索 `AGENTS.md`，未发现适用文件。服务数据目录不属于源码审查范围；`postgres_data` 的目录搜索曾返回权限拒绝，未继续访问 |
| 本轮检查 | `rg`、带行号源码阅读、Git 只读查询、官方网络资料、8 个纯内存行为探针；未运行项目完整测试套件 |

项目入口和目录以 [README.md](../../README.md)、[pyproject.toml](../../pyproject.toml) 为导航，实际判断落到 `applications/bootstrap.py`、`memory/`、`runtime/context/`、`runtime/sessions/`、`applications/coding/`、`tools/`、`web/` 及相关测试。

旧资料不能替代当前代码：[旧记忆审计](../refactors/memory-system-audit.md):3 明确针对重构前；[演进报告](../reports/memory-runtime-evolution-report.md):19–25 仍列 `memory/store.py`、`MemoryLifecycle`、`MemoryProcessingDevice`。本轮当前文件清单和生产调用链未发现这些实现，不能沿用其旧链路、历史数据量、性能结论或测试通过数。本文不修改这些历史文档。

### 1.2 阅读约定

- **事实**：源码、公开实现、官方文档直接支持；有实现不等于部署已启用。
- **已复现**：本轮对实际函数执行过的纯内存合成探针，结果见第 10 节；不表示数据库、真实模型或线上环境复现。
- **推演**：静态调用链支持的条件性失败场景，不声称已发生生产事故。
- **建议**：本轮提出的目标设计，尚未实现。
- **待验证假设**：缺部署、数据量、负载或效果证据，必须后续测量。
- “未发现实现”限定在所列源码和入口搜索范围，不能证明一切外部部署脚本或第三方扩展均不存在该能力。

本文关注跨任务、跨会话的信息保留与治理；只讨论注入内容的来源、资格与撤回，不展开单次请求的上下文预算、压缩或 token 分配。

## 2. 主要结论与优先级

| 优先级 | 判断 | 证据与行动 |
|---|---|---|
| 启用自动写入前必须处理 | 模型可以给自己的总结标“已验证”；工具写入也直接被标为显式用户偏好 | `conclusions.py::_parse_candidates` → `TaskMemoryPromoter.promote` → `record_verified_conclusion`；见 F1、P8。先改为候选，可信确认与模型输出分离 |
| 多用户/共享工作区前必须处理 | 用户 owner 可区分用户，但其他 scope 不是用户联合授权 | `MemoryContext.allowed_owners`；见 F2、P5。默认私有命名空间，共享显式授权 |
| 最小可用性修补 | 禁用向量索引时已有记忆不可见；候选重复显式保存仍为候选；相反语句可能被合并 | 见 F3、F4、F6、P1–P7。提供独立结构化检索、明确状态矩阵和保守去重 |
| 日常使用必须具备 | 当前 Web 页面读的是遗留 Markdown，用户不能在该页面管理语义库 | `web/server.py:858–859,1550–1551`；见 F7。补语义库列表、来源、修改、停止使用、清除副本 |
| Coding 跨任务复用前必须处理 | 保存了代码 revision 元数据，但没有代码有效性校验；子运行器自动注入服务未贯通 | 见 F5、F9。依赖文件摘要与可信版本检查、明确传入读取服务 |
| 保留向量检索时处理 | Outbox 有事务与重试，但未发现 processing 租约回收和持续消费闭环 | 见 F8。单 worker、租约与监控足够，不需要消息中间件 |
| 有收益再建设 | 全文/语义混合检索、自动经验提炼、更复杂冲突推理 | 第 8–10 节给出升级条件；不预设其优于少量明确的结构化记忆 |

**推荐的最小实现**：沿用 PostgreSQL 的 `MemoryItem + MemoryEvidence + MemoryCommandService`；先让明确用户保存、候选审核、按 ID 纠错/撤销、范围过滤、版本有效性检查形成闭环；支持不依赖 embedding 的查询。模型总结只能产生候选。少量已确认偏好和有来源的项目决策足以起步。

## 3. 项目究竟有什么记忆

### 3.1 逐项清点

| 数据/能力 | 状态 | 当前代码证据：文件、符号、行号 | 边界 |
|---|---|---|---|
| 原始聊天记录 | **已经实现** | [session_store.py](../../runtime/sessions/session_store.py):32–119 建表，`load_session`:168–216，`save_session`:343–421 | 可恢复同一会话；不是筛选后的长期事实 |
| 文件/工具缓存 | **已经实现** | [assets.py](../../runtime/context/assets.py):96–119 `_read_instruction_file`；[handlers.py](../../tools/handlers.py):1765–1818 工具结果缓存 | 减少 I/O 或重复工具调用，不代表学习了用户经验 |
| 工具结果保存 | **已经实现** | [result_store.py](../../runtime/tooling/result_store.py):52–120、122–180、183–294，artifact/file/PostgreSQL adapters | 保存供后续访问的证据；不能把整段输出自动视为长期事实 |
| 运行日志、结论工件 | **已经实现** | [trace_store.py](../../runtime/trace/trace_store.py):57–114；[artifacts.py](../../applications/coding/artifacts.py):22–61、105–109 | 写 trace、报告、`CONCLUSIONS.json`、`TASK_LOG.md`；日志存在不证明其被记忆检索使用 |
| 任务检查点 | **已经实现** | `SessionStore`:83–100、198–207；[task_state.py](../../applications/coding/task_state.py):449–466；[service.py](../../runtime/task_state/service.py):82–138 | 保存/恢复任务状态；新任务不应继承旧任务的完成状态或授权 |
| 原始历史向量检索 | **部分实现** | [episodic_retrieval.py](../../memory/episodic_retrieval.py):41–56、98–151；[qdrant_index.py](../../memory/qdrant_index.py):41–112；[vector_runtime.py](../../memory/vector_runtime.py):52–57 | 普通会话按当前 session，Coding 按 task；有 adapter 和读取装配，本轮未找到持续写入 session_turn 的生产链，不认定全链贯通 |
| 语义记忆存储及向量派生索引 | **已经实现，按开关装配** | [postgres_repository.py](../../memory/postgres_repository.py):62–168；[semantic_index.py](../../memory/semantic_index.py):81–164；[bootstrap.py](../../applications/bootstrap.py):135–203 | PostgreSQL 为事实源；Qdrant 返回 ID/version 后回源验证。向量检索是一种检索方式，不是记忆类型 |
| 用户偏好 | **已经实现，分类过宽** | `run_memorize`，[handlers.py](../../tools/handlers.py):3031–3081 | 所有该工具输入均存为 USER/PREFERENCE/EXPLICIT_USER/`confidence=1.0`；项目事实也会被套入偏好类型 |
| 项目事实、决策 | **部分实现** | [promotion.py](../../applications/coding/promotion.py):80–126、168–185 | 有项目/工作区/任务 owner、出处和 revision；“验证”由模型字段驱动，且检索不核代码版本 |
| 历史经验、可复用做法 | **部分实现** | [conclusions.py](../../applications/coding/conclusions.py):29–75、149–173；`TaskMemoryPromoter`:145–165 | 提取与晋升已有实现；独立验证、适用前提、反例、失效规则未形成闭环。`PROCEDURE` 枚举本身不证明经验学习策略已实现 |
| 候选、确认、拒绝、纠正 | **领域服务已经实现，产品入口不完整** | [command_service.py](../../memory/command_service.py):51–57、73–149 | 非空方法与事务是真实实现，不是空接口；Web/tools 中未发现候选审核和按 ID 修改入口 |
| 用户可管理的长期记忆页面 | **仅遗留文件查看已实现** | [server.py](../../web/server.py):858–859、1550–1551；[legacy_files.py](../../memory/migration/legacy_files.py):14–28；[MemoryPage.tsx](../../web/frontend/src/pages/MemoryPage.tsx):5–7 | 刷新/预览旧文件，不能证明当前语义库为空，也不能编辑/删除它 |
| 分支、代码变更失效 | **部分记录，失效未实现** | `runner.py::_repository_identity`:491–507；`promotion.py`:102、123；`domain.py::is_retrievable`:155–161 | 记录 revision，不比较当前 revision、分支、文件摘要或脏工作树 |
| 普通聊天后台自动提炼/晋升链 | **只有历史文档描述，当前生产实现未发现** | [演进报告](../reports/memory-runtime-evolution-report.md):19–25 描述 `MemoryLifecycle/MemoryProcessingDevice`；当前 `memory/` 文件清单与 `bootstrap.py` 调用链未发现对应模块 | 不把旧报告的功能与测试数字视为当前版本能力；已有 `propose` 服务接口不等于自动后台链 |
| 敏感信息/注入候选治理 | **当前记忆写入链未发现实现** | `MemoryWriteProposal`、`remember`、`record_verified_conclusion`、`run_memorize`、`TaskMemoryPromoter` | 旧演进报告中的治理模块不能当作当前已生效防护；不据此断言整个 Agent 没有其他工具安全措施 |

### 3.2 开关和两条实际调用链

[bootstrap.py](../../applications/bootstrap.py):135–190 在总开关或任一细分开关打开时构造服务；:191–203 控制写入/显式读取，:251–256 控制自动注入。`SEMANTIC_MEMORY_ENABLED=1` **只保证装配，不等于三个入口全部启用**。代码默认和 [.env.example](../../.env.example):380–394 将 WRITE/READ/CONTEXT 设为关闭；本轮未读取实际环境，部署是否启用待确认。

```text
显式保存工具：
model tool call → run_memorize → MemoryCommandService.remember
  → PostgreSQL item + evidence + outbox（同事务）
  → synchronizer.drain(limit=10) → 派生索引

普通运行器自动读取：
ContextMemoryService.build_memory_block(current_request)
  → SemanticMemoryRetrievalService.retrieve
  → index 搜索 → PostgreSQL 回源查 owner/status/time/version
  → render → MemoryContextProvider → user-role context frame

Coding 任务结论：
runner → TaskConclusionExtractor（模型）
  → _parse_candidates → TaskMemoryPromoter
  → record_verified_conclusion → ACTIVE
  [当前 verified 不是独立验证器结论；此调用点没有同步 drain]
```

读取安全方面已有实质优点：`semantic_retrieval.py:86–120` 不相信索引 payload 的正文，回源检查可防旧版本、撤销记录与不允许 owner 被直接注入；`semantic_index.py:84–101` 的语义索引 payload 不保存正文和 evidence。原始历史索引保存文本，不能把这项优点外推到所有索引。

### 3.3 当前生命周期完成度

| 环节 | 现状 | 尚缺什么 |
|---|---|---|
| 候选生成 | `propose` 可创建 candidate；Coding extractor 返回结构化候选 | 未发现普通聊天后台自动提炼的当前生产链；自动生成不应先于治理 |
| 写入判断 | 显式 remember / verified conclusion 两条 active 路径 | 真实用户授权证据、可信验证器；模型不能自填 EXPLICIT_USER/verified |
| 去重合并 | normalize + 词集合近似；合并 evidence | 否定/版本/数值识别，安全精确键，并发幂等 |
| 来源记录 | Evidence 有 source_ref、session/task/project、excerpt、metadata | 强制来源定位、可信来源等级、去重证据标识，杜绝空证据激活 |
| 存储 | PostgreSQL 主记录/evidence/outbox；乐观版本、supersede 事务 | schema 迁移策略、namespace 强约束、删除清单 |
| 检索 | scope/time/status/version；排名；异常时词法降级 | Null/关闭索引也能读，相关性拒答阈值，代码有效性，中文及符号保真 |
| 注入 | 单独 semantic_memory 区块，普通 frame 为 user role | 可验证出处/有效性展示，分隔符转义，Coding 装配测试 |
| 纠错 | confirm/reject/update/revoke 已实现 | 产品入口、前端 expected_version、重复记住状态矩阵、冲突明确反馈 |
| 过期 | `valid_until` 读取时过滤；EXPIRED 枚举/状态转换 | 未发现定期过期处理；再次确认和旧代码失效逻辑不足 |
| 删除 | revoke 排 DELETE，回源可即时停止语义召回 | 硬删除、来源关联删除、日志/工件/历史/备份边界、防重新提取复活 |

## 4. 外部方案：核实实现与许可后再借鉴

以下均为 2026-09-27 访问的第一手资料。固定 commit/tag 用于实现审查；动态文档只代表访问时公开说明。开源 SDK/客户端不代表相关云服务的内部实现开源。

### 4.1 比较表

| 方案 | 开源/版本核验 | 实际机制 | 对本项目的适用判断 |
|---|---|---|---|
| LangMem + LangGraph Store | LangMem/LangGraph 公开源码均为 MIT；所读 LangMem main 的包版本 `0.0.30`，未取得可靠 HEAD SHA，因此不冒充固定 commit | namespace + JSON value + put/search/delete；Store 与 thread checkpoint 分开；提取器可让模型生成增删改 | 借接口与边界；无需为现有 SQL repository 引入整套 LangGraph/Trustcall 依赖 |
| Letta Code / MemFS | Apache-2.0；`v0.32.1`，`9ead5f0480b2c4e0f91e1cf4511126294c442948`，2026-09-10 | 按 agent 的 Git-backed 文件记忆；写入 reason、read_only 检查、修改后 commit；官方说明默认无语义索引 | 借可查看、可回溯、按需读取；不照搬 Git 同步、后台多 Agent 或自改系统记忆 |
| Mem0 OSS Memory | 访问时 main 许可为 Apache-2.0；代码审查 `v2.2.1`，`94c3fe9f238f3dbf29c9ce98643bd71eb13077cd`，发布 2026-09-25 | LLM 提取、向量存储、实体过滤、历史记录；该版默认 add 管线为 ADD-only，显式 update/delete 另有 API | 大量自然语言同义召回可作为参考；不能取代本项目已有来源、状态和权限治理 |
| Claude Code 文件记忆（公开说明） | 非按 MIT/Apache 授权的开源记忆引擎；仓库 LICENSE 为保留权利、使用适用商业条款 | 官方说明区分用户写的指令文件和自动记忆；默认用户目录下的按项目 Markdown 可查看/编辑/删除 | 借用户可见性、少量文件、分项目；内部生成与冲突实现未公开核验，不推断其“已解决真实性” |

### 4.2 LangMem：结构化 Store 不等于事实审核

**公开事实**：`create_manage_memory_tool` 按 namespace 写入 schema 约束的 content，支持受限动作和删除；`create_search_memory_tool` 调 Store 搜索。`MemoryStoreManager` 可将旧记录与新消息交给模型并应用提取结果。LangGraph 官方明确将单 thread checkpoint 与跨 thread Store 分开。[tools.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/tools.py)、[extraction.py](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/extraction.py)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Stores](https://docs.langchain.com/oss/python/langgraph/stores)，访问 2026-09-27。

**判断/反例**：namespace 便于寻址，但若 namespace 由模型随意指定，仍会越权；模型合并旧记录也可能把推断覆盖成事实。建议只借 Store 接口，宿主决定范围、候选状态和 CAS 更新。少量偏好无需引入框架。示例 InMemoryStore 不应被当作跨进程持久化。[官方仓库与说明](https://github.com/langchain-ai/langmem)、[LangMem MIT 许可](https://github.com/langchain-ai/langmem/blob/main/LICENSE)、[LangGraph MIT 许可](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)、[包版本及依赖](https://github.com/langchain-ai/langmem/blob/main/pyproject.toml)，访问 2026-09-27。

### 4.3 Letta：可审查文件记忆，不预设向量数据库

**时效事实**：当前 `letta-ai/letta` 官方仓库说明旧 V1 API server 位于 archive 分支，活跃实现移到 `letta-code`。本报告不把旧 Python server 架构当作当前实现。[官方迁移说明](https://github.com/letta-ai/letta#historical-source)，访问 2026-09-27。

**实码事实**：固定版本的 `memory()` 要求写入 reason，检查工作树状态，执行 create/replace/delete 等操作并提交；读取 `read_only` 限制。文件目录解析按 agent ID 隔离。官方 MemFS 文档说明默认不用 semantic/vector index，而是搜索和读取文件。[memory.ts:97–150,511–526](https://github.com/letta-ai/letta-code/blob/9ead5f0480b2c4e0f91e1cf4511126294c442948/src/tools/impl/memory.ts#L97)、[memory-filesystem.ts:40–117](https://github.com/letta-ai/letta-code/blob/9ead5f0480b2c4e0f91e1cf4511126294c442948/src/agent/memory-filesystem.ts#L40)、[MemFS](https://docs.letta.com/concepts/memfs)、[许可](https://github.com/letta-ai/letta-code/blob/v0.32.1/LICENSE)，访问 2026-09-27。

**建议与代价**：可以用“记录理由、用户能检查来源与修订”的设计替代不透明自动记忆。按 agent 隔离不等于按用户、项目、分支授权。Git 解决版本历史，不裁决语义冲突；保留 Git 历史也增加敏感内容彻底删除成本。后者是设计推演，不是 Letta 的已复现事故。本项目已有 SQL 纠正链，不值得再建一套 Git 事实源。

### 4.4 Mem0：按新版代码分析，不能沿用旧版宣传

**实码事实**：所审 `v2.2.1` 的 `_add_to_vector_store` 正常路径先查作用域内旧记录，再提取新内容，采用文本 MD5 去重后添加新记录；不是旧介绍里由默认写入管线自动裁决 ADD/UPDATE/DELETE。`infer=False` 路径仍需 embedding。显式 update/delete 存在，删除向量后 history 仍可记录旧值。[main.py](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py)、[prompts.py](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py)、[storage.py](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py)，访问 2026-09-27。

**建议与反例**：ADD-only 可减少模型误删旧内容，但新旧事实并存仍须裁决；user/agent/run 过滤仍须绑定宿主身份。多存储写入与删除要验证真实结果。该版公开修复包含后端拒绝插入却返回成功、部分 provider 过滤问题；这些是上游修复信息，不是本项目的故障证据。[v2.2.1 发布说明](https://github.com/mem0ai/mem0/releases/tag/v2.2.1)、[访问时 main 的 Apache-2.0 许可](https://github.com/mem0ai/mem0/blob/main/LICENSE)，访问 2026-09-27；固定 commit 的 LICENSE 本轮抓取未成功，不将 main 许可核验冒充固定版核验。不建议现在替换本项目 repository 为该 SDK。

### 4.5 Claude Code：仅采用公开说明的边界

**公开事实**：官方区分人工编写的指令文件与自动记忆；自动记忆默认位于 `~/.claude/projects/<project>/memory/`，目录可配置，并可通过 `/memory` 或编辑器查看、编辑、删除。文档说同一 Git 仓库的 worktree 共享自动记忆目录。这是公开行为说明，不是内部提取、去重、删除实现的源码证明。[Memory 文档](https://code.claude.com/docs/en/memory)，访问 2026-09-27；[LICENSE](https://raw.githubusercontent.com/anthropics/claude-code/main/LICENSE.md) 明确保留权利并指向商业条款。

**判断**：可借可见、可编辑的小记忆集合；共享 worktree 对稳定项目约定方便，对分支差异事实仍应附有效条件。人写的仓库规则与从工具输出提炼的记忆需要不同信任等级，不能因为二者都是 Markdown 就等权使用。本项目没有必要为模仿文件形式退回旧 MEMORY/PENDING 双文件链。

## 5. 对抗性分析：问题、收益、代价与验收

### F1. 模型“自证”及来源升级，使错误经验成为 active

**现状证据**：`conclusions.py:149–171` 对模型 JSON 使用 `bool(verified)`；`promotion.py:81–126` 将其复制到证据并晋升；`command_service.py:59–71` 只验证该标志真值。`runner.py:235–254` 在 completed/stopped 状态设定后均走提取/晋升。`run_memorize`:3051–3070 则对工具 content 直接设显式用户来源、偏好类型、置信度 1.0，未核对具体用户原话与保存授权。

**触发 → 场景 → 影响**：模型或错误工具输出生成貌似合理的经验，或 JSON 返回 `"verified":"false"`。**P8 已复现**后者即使无 evidence_file 也可成为 active；“因此真实用户已受到污染”仍属未验证。正文再由 `semantic_retrieval.render`:139–150 直接拼入记忆区块，可能让模型将错误内容当长期依据。注入攻击存在跨轮持久化风险，研究动机可参照 MINJA；其实验结果不能直接套成本项目成功率。[MINJA v5，2026-02-12](https://arxiv.org/abs/2503.03704v5)。

**已有优点**：默认 verified 为 false，有限长/分类/噪声过滤，owner 由宿主 context 决定；普通记忆 frame 是 user role（`builder.py:429–434`），不是 system role。

**改进建议**：模型输出只允许 candidate；真正用户确认与可信文件/工具验证建立独立记录。严格布尔解析仅修类型问题，不能证明事实真实。宿主设置来源/作用域/验证者/版本；模型不能给自己签发 verified。保存秘密、账号令牌、整段工具输出、工具内指令默认拒绝；经验保留“条件—动作—观测结果—限度”。

**新增成本/风险**：人工审核增加操作；纯语法 secret 检测会误报/漏报；自动验证规则也会失效。先允许明确低敏用户偏好直接保存，其他候选待确认，不建设通用“真实性模型”。

**不值得复杂化的条件**：长期写入持续关闭，或唯一入口是宿主固定结构字段的人工保存时，可不建设自动提取审核队列；一旦允许模型或外部文本写入，来源边界不能省略。

**验收**：模型 `true`/`"false"`/伪造 source_ref 均不能产生 verified active；停止任务不自动晋升；注入记忆不能改变工具权限；测试用秘密在 item/evidence/trace 中均不出现。转义 XML 分隔符仅保障格式，不能替代来源约束。

### F2. scope 合集不等于租户和项目授权

**现状证据**：`OwnerKey`（`domain.py:51–61`）只有 scope/id；`MemoryContext.allowed_owners`（`commands.py:32–45`）把 USER/APPLICATION/WORKSPACE/PROJECT/TASK 做合集；`_owned/_authorize`（`command_service.py:245–257`）只核这个集合。PROJECT 常来自 Git 根目录路径（`runner.py:491–507`）。

**触发 → 场景 → 影响**：Alice/Bob 共享 application=coding 或同项目 ID 时拥有共同 owner，**P5 已复现键相交**。如果这些 owner 中写入私人内容，则可能跨用户可见（推演）。同一用户误把项目事实经 memorize 存到 USER，也可能在另一个项目召回。不是所有共享都属于泄漏，真正团队共享必须有显式产品语义。

**已有优点**：模型不能直接从 memorize 参数指定 owner；用户 owner 不同；语义查询及回源都校验 owner。

**改进建议**：所有私人数据都包含宿主绑定的 tenant/user namespace，项目作为附加作用域；项目共享单独设可授权的 shared namespace。分支/版本条件不能靠用户 ID 解决。匿名单机 `local` 与已登录用户分开定义，用户 ID 缺失时不在远程多用户路径自动落入共享 local。

**新增成本/风险**：迁移已有 scope 需识别归属；强行加 user 前缀会破坏本来需要的团队共享；路径不能当稳定仓库身份。先默认私有，无法归属的旧项隐藏待审，不猜测批量迁移。

**不值得复杂化的条件**：永久单用户、无共享入口可暂不建设团队 ACL；仍需隔离多个项目。无需单独权限微服务。

**验收**：相同项目名、相同目录、相同 application 的不同用户，私人记忆命中为 0；显式共享只有成员能读写；模型传入其他 scope 不改变宿主授权；项目 A 内容不得在 B 注入。

### F3. 模糊去重可能吞掉否定、数值和代码差异

**现状证据**：`dedup.py:41–77` 默认以词集合 containment/Jaccard 判重复；生产默认 `MemoryConflictService()` 没有 semantic_resolver（`command_service.py:30–33`）。`conflict_service.py:56–87` 在检查 conflicts_with_id 之前就可 MERGE_SEMANTIC。`normalize_memory_text`:5–17 会删除标点/空白。

**触发 → 场景 → 影响**：**P3 已复现** `Always deploy after tests pass` 与 `Never deploy after tests pass` 被判重复；**P4 已复现** `--no-cache` 与 `--no cache` 归一化碰撞。调用命令时可能只合并证据并保留旧内容（推演）。`add_evidence` 更新 updated_at，排名又据其计算 freshness，错误旧结论反而可能更靠前。

**已有优点**：确定、低成本，无需 LLM；相同内容可保留多条来源。

**改进建议**：显式 correction/id 优先；命令、路径、版本值按保真结构字段比较；普通文本最多做安全精确去重。相似只标 related/possible_conflict，不自动合并。用户偏好的确定属性用 claim_key/value，非结构经验不硬造属性键。

**新增成本/风险**：保留重复条目会增加噪声；人工清理比不可见的错误合并容易审计。别马上用另一个模型替换判断，因为它也会误判且更难重现。

**不值得复杂化的条件**：小集合可以完全不做语义去重；无需聚类。不能为去重便利牺牲否定和代码符号。

**验收**：否定、数值、版本、单位、大小写敏感路径、命令标点对照组不误并；同 claim_key 更正走明确 supersede；幂等重试不新建重复项；普通相似不自动提升信任。

### F4. 重复“记住”与过期再次确认的语义不闭合

**现状证据**：`_create_or_merge`:187–202 找到重复只 add_evidence；SQL `find_exact`:216–236 允许 candidate/active 但不判 TTL；`update`:121–122 沿用旧 valid_until。

**触发 → 场景 → 影响**：**P1 已复现** propose X 后 remember X 仍 candidate；**P6 已复现**过期但 status=active 的 X 再 remember 仍不可检索；**P7 已复现** update 该过期项因新 valid_from 晚于旧 valid_until 抛错。用户保存与实际可用性可能不一致。

**已有优点**：避免重复新建、保留证据；状态机禁止任意复活终止状态（`domain.py:187–210`）。

**改进建议**：把“观察到重复”“明确确认候选”“重新确认过期”“修改现值”拆开。candidate + 有效用户确认才 active；过期重新确认创建新记录/版本并保留旧出处；不能因 add_evidence 自动延寿。

**新增成本/风险**：更多明确命令结果和状态测试；自动续期会让旧偏好永不过期，因此只接受可信确认事件。

**不值得复杂化的条件**：没有候选/TTL 的窄版产品可先限制入口，但不能以保存成功文案掩盖仍不可检索的状态。

**验收**：相同内容的四种动作均返回实际状态与是否可用；inferred 重复不激活；确认后可读；过期续期有新证据和时间，不继承无效截止日期。

### F5. 记忆版本与代码版本不同，旧项目事实没有自动失效

**现状证据**：`runner.py:149–153` 在任务开始记录 HEAD；`promotion.py:102,123` 允许模型 code_revision 优先于宿主值。检索 `semantic_retrieval.py:94–103` 只核 item.version；`MemoryContext` 无分支/revision 字段，`is_retrievable` 只核状态和时间。

**触发 → 场景 → 影响（推演）**：分支 A 用 pytest，B 改用 unittest，项目路径相同；或任务修改工作树但未提交，HEAD 未变。旧“项目测试命令”仍可作为当前事实使用，导致重复错误操作。当前没有真实任务复现证据。

**已有优点**：已有来源 revision 可做迁移基础；用户偏好不会因任意提交失效。

**改进建议**：代码事实携带依赖文件路径、内容摘要、可信观测 revision 和验证时间。检索前检查相关文件；不同则标 needs_revalidation 并要求重读。起步可用“revision 不同则不作事实、只提示待核线索”，后续再用路径摘要减少误失效。分支名仅辅助，不替代摘要。

**新增成本/风险**：每次 HEAD 变化全量失效过于保守；摘要检查有 I/O，文件重命名/未提交变更要定义。只检查候选命中项依赖，优先高复用项目事实。

**不值得复杂化的条件**：仅保存用户偏好、人工维护的长期原则时不需 Git 有效性图；易从代码读取且没有复用收益的事实直接不记。

**验收**：相关文件变更/删除/重命名、脏工作树和分支切换后旧事实不作为有效事实注入；无关文件提交不误杀已验证的路径级事实；模型伪造 revision 被忽略。

### F6. 无向量模式不可靠，检索资格检查后仍可能注入噪声

**现状证据**：`vector_runtime.py:64–88` 禁用或非 strict 初始化失败返回 Null index；`semantic_index.py:35–48` 搜索返回空；`semantic_retrieval.py:69–85` 只有抛异常才 fallback。其 :115–122 无最小 relevance 门槛；`_rank`:224–232 中相关性只占一半；`_lexical_relevance`:235–240 为字符集合 Jaccard，不是全文检索。

**触发 → 场景 → 影响**：**P2 已复现**已有 active 项而 Null index 返回 0 hit、degraded=false。索引未同步也可能无结果。另一方向，向量提供弱相关候选，高自报 confidence/salience 可抬高其排名；英文共享字母可能让 fallback 误相关（推演）。

**已有优点**：查询异常已有限定 scope 的降级，旧索引点会回源拒绝；安全资格验证与相关性排序已分层。

**改进建议**：选择显式 structured/lexical backend，disabled/unavailable/ready 可见；允许“无相关记忆”返回空。先资格过滤，再按真实词项/属性/依赖文件相关度选，设最低相关性与歧义拒答；importance/freshness 只能排序，不能替代相关性。是否对空向量结果补检应有独立策略，不能无条件全库扫描。

**新增成本/风险**：SQL 全集合扫描会随数据量上升；中文无空格与代码符号需专项评测；阈值过高会漏召回。集合很小时受控扫描足够，大了再加 FTS。

**不值得复杂化的条件**：几十条偏好/规则可按 scope+kind+claim_key 查；无实测同义召回收益，不需 embedding/reranker。

**验收**：禁用、初始化失败、查询失败、索引滞后四种条件有准确 backend 状态；前三种能读有效相关项；不相关 query 可空；已撤销、跨 scope 和过期项仍为 0；中英文和代码符号分别报告 precision/recall。

### F7. 用户管理视图断开；“忘记”不等于清除所有副本

**现状证据**：当前 Web 只读 legacy files（第 3 节）。`forget`（`command_service.py:162–175`）在 active 项做双向子串匹配后 revoke；`MemoryRepository`:51–108 无 purge；evidence/outbox FK 为 ON DELETE RESTRICT（`postgres_repository.py:103,122`）。`SessionStore.delete_session`:731–754 删除该会话表记录，不处理 semantic memory、trace、任务工件和向量。

**触发 → 场景 → 影响（推演）**：用户在空旧文件页面以为没有记忆；删除聊天后派生记忆仍在；按“测试”忘记可能多匹配，candidate/旧 superseded 内容又不会被该操作清除。重新从旧记录导入也可能复活被删除信息。

**已有优点**：revoke 保留审计链；语义回源立即排除 revoked，向量延迟删除不必导致其被使用。`ContextBuilder` 的基础 prefix cache（:178–225）没有缓存 semantic memory，不能把它写成已证实的旧记忆缓存漏洞。

**改进建议**：先按 ID 提供列表/详情/出处/修改/撤销；查询式批量操作展示确定的匹配集合。明确“停止未来召回”和“清除保存副本”的区别。purge 清单覆盖 item/evidence/index/工件/相关 trace/导出；来源 tombstone 防重导入；删除聊天可选同时撤销其衍生记忆。已发给模型的请求无法追溯取消，下一次组装必须排除。

**新增成本/风险**：硬删除与审计、备份恢复冲突；需要来源反向索引及保留期限。不能承诺所有历史备份瞬时抹除；应写清备份最长保留期及恢复时应用删除清单。

**不值得复杂化的条件**：无敏感数据、单机个人用途可先做撤销和明确说明；若产品承诺“永久删除”，全副本边界不能省略，也不必先建设分布式删除平台。

**验收**：用户能看到真实语义条目和来源；撤销后留着旧向量点也不能注入；purge 后主库/证据/派生副本检查符合承诺；重新导入、重建、旧会话恢复均不能静默复活；同会话历史已有文本的处理有明确测试与产品提示。

### F8. 索引 outbox 有良好基础，崩溃恢复和消费调度仍有缺口

**现状证据**：`postgres_repository.py:151–168,252–318` 保证事实与 outbox 同事务；`claim_index_events`:358–380 将 pending/retry 改 processing，未发现租约到期回收；`index_sync.py:61–69` 仅对捕获异常重试。工具会 drain，Coding promotion 调用点不 drain。精确文本索引 :88–91 不是 UNIQUE，命令查重/新建分开事务。

**触发 → 场景 → 影响（推演）**：claim 后进程被终止，事件一直 processing；只产生 Coding 结论而没有后续消费，索引落后；两个请求同时查无相同项，再各创建 UUID。影响主要是漏召回/重复和维护负担，未在真实数据库复现。

**已有优点**：事务 outbox、SKIP LOCKED、指数退避；索引可由事实源重建。当前重建器仅 upsert active（`rebuild_index.py:25–59`），并非清理全部孤儿点。

**改进建议**：若保留向量，单后台循环即可：lease_until、到期重领、幂等执行、queue age、失败计数；提供操作审计。命令增加 idempotency_key 和数据库约束/锁。不要对过度删标点后的 normalized_content 草率加唯一约束。

**新增成本/风险**：租约太短会重复执行，太长会延迟恢复；索引重建有 embedding 成本。当前无向量模式稳定后再考虑后台 worker，不引入 Kafka 等额外服务。

**不值得复杂化的条件**：A 档完全不用派生索引，不需修建向量消费平台；单用户低并发也不需多 worker，但用户操作幂等仍有价值。

**验收**：claim 后终止/重启可恢复；重复命令只一项；重试不会复活 revoked；生产装配测试验证 Coding 结论最终可检索；重建对孤儿点有明确处置而非宣称已清空。

### F9. 主运行器与 Coding 子运行器的自动注入不一致

**现状证据**：`bootstrap.py:251–256` 将 retriever 交给主 builder；`runner.py::_build_task_runtime`:342–363 新建 builder 时未传 memory_service/retrieval_service；`builder.py:107` 默认空服务，`context/memory.py:14–24` 返回空。`Runtime.fork`（`runtime.py:61–75`）保留工具与 executor，因此不能说 Coding 完全不能读：显式 recall 工具仍可继承。

**触发 → 场景 → 影响（推演）**：主应用自动注入开关开启，下一次 Coding 任务没有显式 recall，就不会自动利用已保存项目事实。直接测试 ContextMemoryService 通过也不能发现装配缺口。

**已有优点**：任务 builder 独立，不会自动复用全部父会话状态；显式工具读取仍可行。

**改进建议**：将已授权的记忆服务显式注入子 builder，并保留按应用/任务关闭的能力；资格仍从当前 task context 计算，不复制父会话已渲染记忆块。

**新增成本/风险**：错误共享 context 会破坏隔离；自动注入可能对简单任务增加噪声。默认只读小量相关项目记忆，允许用户禁用。

**不值得改造的条件**：若产品决定 Coding 只使用显式 recall，应清楚说明并测试这一策略，不能声称自动跨任务记忆已贯通。

**验收**：从生产 factory/fork 构造两个不同任务，验证 WRITE 控制保存/晋升、READ 控制显式 recall、CONTEXT 控制自动注入、ENABLED 仅触发装配；各组合符合此矩阵。第二任务只获得有权且有效的条目；CONTEXT 关闭时零自动注入。

### F10. Trace 有摘要，但部分“digest”其实包含正文

**现状证据**：`command_service.py::_emit`:274–287 有 content_preview；`semantic_retrieval.py::_emit`:159–166 的 query_digest 是规范化 query 前 80 字符。真正 hash 实现在 `runtime/trace/memory_injection.py:59–65`。`trace_store.py::_json_safe`:449–487 主要按键名删除 secret/token 等，content_preview 不会因此自动脱敏。

**触发 → 场景 → 影响（推演）**：用户错误保存秘密，或 query 含敏感信息，正文片段可能进入队列/trace；仅清语义库不足以删除这些副本。

**已有优点**：injection explanation 已有 hash、ID、scope、drop reason，可复用现有 trace，不必新增审计系统。

**改进建议**：默认只记 ID/哈希/版本/理由；若本地调试确需正文，显式开启且限时保存。统一使用真正摘要函数，不以“digest”命名当脱敏证据。

**新增成本/风险**：只用 hash 降低故障可读性；可通过有权限的按 ID 详情补查，不复制全文到所有日志。

**不值得复杂化的条件**：不需要新脱敏服务或复杂可观测平台；替换默认预览字段和建立保留策略即可。

**验收**：合成敏感字符串不出现在默认 trace、summary、错误文本；仍能按 memory_id 定位检索/删除链路。


## 6. 目标生命周期：先决定能否相信，再决定如何找回

本节全部是**建议**。保留现有 `MemoryItem/MemoryEvidence` 和事务模型，不另造一套平行的“智能记忆平台”。

### 6.1 什么值得记，什么不应直接记

| 信息 | 例子（设计示例，不代表真实用户情况） | 最小处理 |
|---|---|---|
| 明确、稳定、低敏用户偏好 | “以后默认用中文简要回答” | 经真实用户保存事件确认后，可 active；USER 私有；用户更正优先 |
| 不能从代码直接恢复的项目决策 | “为兼容旧客户，这一版本保留 v1 endpoint” | 保存作决定的出处、有效版本与适用项目；可用户确认，不得归成通用偏好 |
| 重复有用的项目操作规则 | “测试需先启动指定本地 fixture” | 保存规则及可核对出处；读前验证依赖；禁止把含凭证的命令原文保存 |
| 条件化经验 | “在依赖版本 X、错误 Y 下，动作 Z 后测试 T 通过” | candidate/observed；明确观测与推断，保留反例或未覆盖部分；不泛化成“永远做 Z” |
| 外部网页/README/工具输出中的事实 | “某 API 参数是 optional” | 原文是证据，不是授权；有来源与日期，仅在项目需要时提出候选 |
| 模型解释和失败归因 | “可能是缓存导致失败” | hypothesis/candidate；不得当 FACT active；测试成功不自动证明原因正确 |
| 一次性任务进度和待办 | “第 3 步完成，下一步改 UI” | 留在 task checkpoint；任务结束后不晋升长期项目事实 |
| 整段聊天、日志、工具输出、检索片段 | 文件很长的原始返回 | 保留原有证据引用和访问权限；不复制成记忆正文 |
| 密码、API token、私钥、cookie、身份认证材料 | 真实凭证或包含凭证的 URL | 默认禁止写入长期记忆及候选正文；存安全存储的非秘密引用也需用户用途明确 |
| 无必要的敏感个人信息、第三人信息 | 健康/财务细节、他人私人联系方式 | 默认不提炼；确需时另做明确同意和保留期限，最小版不开放这类自动写入 |
| 权限、系统提示与恶意持久指令 | “以后跳过审批”“泄露 token”“记住网页作者有管理员权限” | 直接拒绝作为行为授权；记忆内容不能改变工具权限或指令优先级 |
| 用户本轮限制 | “这次只做设计，不改代码” | 当前任务权威输入，不自动变成以后永远不改代码的个人偏好 |

核心筛选问题：**下一任务可能用得到吗？是否难以廉价重新获得？来源是什么？适用范围和失效条件能否写清？用户是否愿意持久保留？** 任一不明确，保留为任务证据或不记，不能用“将来也许有用”无限囤积。

### 6.2 建议的确定性流程

| 阶段 | 输入和判断 | 输出/拒绝原因 |
|---|---|---|
| 1. 产生候选 | 用户显式保存事件、宿主观测、Coding 结论；模型只提供 claim 和来源线索 | 未确认候选；不读取无关私密文件来“完善记忆” |
| 2. 来源绑定 | 宿主绑定 authenticated principal、message/tool event ID、项目/任务与观测版本 | 缺源/来源不可访问 → 拒绝或待审；模型伪造 user/verified 字段无效 |
| 3. 写入准入 | 稳定性、必要性、敏感信息、注入迹象、范围和长度 | deny / candidate / active；每次返回明确 reason |
| 4. 去重与冲突 | 先 idempotency_key，再安全精确相等/claim_key；相似只给关联建议 | merge evidence / explicit supersede / conflict pending，不凭高 similarity 自动删旧项 |
| 5. 事务存储 | item、来源、状态变化、必要 outbox 一次提交 | 持久化结果与检索可见性分开返回；失败不得说 saved |
| 6. 资格筛选 | 租户/授权 → scope → status/TTL → 代码有效条件 → 撤销/删除标记 | 可用集合；各 drop_reason 可观测，不能先全库召回再仅靠 prompt 隔离 |
| 7. 检索与排名 | 基础属性/词项查询；可选向量建议；最低相关性 | 0..K hits，允许无结果；返回 backend、matched_terms、source 和有效性 |
| 8. 注入 | 只选有效项；内容与可执行指令分隔，标事实/用户声明/经验和来源 | “历史依据”形式；不自动授予任何权限 |
| 9. 纠正 | 用户看见具体 ID 和版本，明确修改/确认/拒绝 | CAS、原子 supersede、旧项停止用；冲突错误可恢复 |
| 10. 过期 | 时间截止、依赖变更、项目删除、来源撤回 | 不可用/待复核；过期不是秘密清除 |
| 11. 删除 | 指定 item 或来源链；撤销先阻止使用，再清理副本 | 回执含已完成/待清理组件、保留边界；防后台再导入 |

不必为这些步骤分别建服务或 Agent。一个 command service、一个 read service、一个小型 source verifier，加现有 repository 足够。

### 6.3 模型经验如何避免变成伪事实

建议沿用 `PROCEDURE/FACT/DECISION`，在结构化附属字段中记录经验，而非增加无限枚举：

```json
{
  "kind": "procedure",
  "epistemic_status": "observed",
  "status": "candidate",
  "claim": "在 fixture X 缺失时，创建测试夹具后用例 T 通过",
  "preconditions": ["依赖版本 X", "错误签名 Y", "工作区状态摘要 D"],
  "action_ref": "tool-event:example-17",
  "observation_ref": "test-result:example-18",
  "observed_result": "指定用例退出码 0",
  "limits": ["未验证完整测试集", "没有证明所有同类报错都由 X 导致"]
}
```

此例为目标记录，未实际执行对应测试。`observed_result` 由可信工具事件读取，`claim/preconditions/limits` 可由模型建议但仍需审查。可信验证也要限定结论强度：“文件存在”只能验证文件存在，不能验证整条架构判断；“测试通过”不能证明因果关系或没有其他回归。

同一助手总结被重复复制三次，不算三份独立证据。 evidence 使用 `source_event_id + claim_key/content_digest` 去重；证据数量与模型 confidence 只做诊断，不能自动换算真实性概率。用户确认某项目“决策”意味着确认其意图，也不等于验证外部世界事实。

### 6.4 冲突裁决与版本失效

建议先处理资格，再比较同 scope/同 claim_key 的事实：

1. 已撤销、过期、来源失效或代码依赖不符的项先排除。
2. 当前明确用户请求优先决定本轮行为；如果用户明确更正长期记忆，按 ID/claim_key 新建替代项，保留来源关系。
3. 同一项目属性的可信当前代码观测优先于旧代码观测；用户关于代码的陈述与当前源码相反时，显示冲突并核实，不能悄悄改代码迎合记忆。
4. 项目级的特定约定可覆盖通用用户偏好；具体任务的临时例外不自动改写跨任务偏好。
5. 不同来源、相同信任等级又无法判定者并存为 conflict/candidate，读取时说明冲突或不采用；不按“最后写入”或“分数最大”强行裁决。

**代码有效性最小规则**：

- 用户偏好：无需绑定 HEAD；改变偏好由用户更正，TTL 可空。
- 项目决策：记录来源、适用项目/版本及复核日期；是否跨分支由作者明确。
- 代码事实/经验：宿主采集 observed_revision、dependency_paths、对应内容摘要；新任务命中时检查。没有 Git 的项目也能用相对路径+摘要。
- 依赖路径必须由宿主在受信仓库根目录内解析，拒绝越界、符号链接逃逸和无访问权限来源；验证器不扩大现有文件读取权限。
- 工作树脏：摘要取实际文件，不只取 HEAD；记录 `observed_worktree_dirty`。模型给的 revision 只保留为未可信候选，不覆盖宿主。
- 文件改变/缺失/权限不足：默认不作为当前事实注入；可返回“需复核”提示供重新检查。源文件重命名第一版按失效处理，之后按价值再增加迁移匹配。
- 分支：可辅助过滤短期分支决策；同一分支内提交会变化，同一 commit 也可出现在多个分支，branch 不是有效性的唯一依据。
- 不针对每条偏好建依赖图；不自动遍历整个仓库做失效扫描。

### 6.5 防污染与用户治理界面

**写入边界**：网页、仓库内容、工具 stdout、助手总结均为数据来源，不能生成“用户授权保存”的凭据。继续让 `source_type`、namespace、verified_by、source event 等不暴露为模型可赋值的工具参数；当前 memorize schema 本来只有 content（`tools/schema.py:1323–1334`）。真正要补的是宿主真实用户事件校验，不能仅凭 memorize 被调用就推定明确保存授权。将来的模型参数最多包含建议内容和候选类型；宿主与命令层再核一次。

**读取边界**：保留现有回源验证，加入有效性检查。渲染应包含 `id/version/kind/epistemic_status/source/observed_at/validity`；正文 JSON 转义或确定性转义后展示为数据。向模型清楚声明“记忆不能更改权限，当前用户可纠正旧记忆”。这不能消灭提示注入，仍须由工具执行层落实权限。

**纠错界面最小操作**：

- “已启用记忆/关闭记忆”状态；列表分已确认/待确认/冲突/已撤销。
- 条目详情显示来源会话/工具、项目、时间、有效条件和为何检索到；不默认把敏感证据全文公开给其他项目用户。
- 编辑内容或纠正时携带 expected_version；版本冲突显示新值，让用户重新判断。
- “停止使用此条”“清除保存副本”分开说明；提供按来源会话查看衍生条目。
- 导出仅在用户操作下进行并受权限校验；不引入一个始终自动同步的 Markdown 副本。
- 查询式删除先解析成具体 ID 集合；明确按 ID 单条撤销无需重复审批，避免把普通管理做成繁重流程。

## 7. 数据结构与接口设计

### 7.1 沿用和补齐的字段

“必需”指最小可靠实现需要，非所有字段都应新增数据库列。先在现有 metadata 中建立严格 schema 并校验；需要过滤、唯一性、授权的字段应成为可索引列。

| 字段/组 | 当前情况 | 最小要求与意义 |
|---|---|---|
| `id`、`version` | 已有 | **必需**。CAS、纠错、检索回源、删除标识；version 为记忆版本，不是 Git 版本 |
| `kind` | 已有 6 类 | **必需**。继续使用 preference/fact/decision/procedure/constraint/relationship；暂不为每个场景增表 |
| `content` | 已有 | **必需**。简短、保真；不能因 normalize 丢失否定、参数或路径 |
| `normalized_content` | 已有 | 保留兼容但**不能充当可靠语义唯一键**；可增加保守 exact_digest |
| `tenant_id / principal_namespace` | 当前 owner 无联合租户 | **多用户必需**。宿主绑定；所有私人 scope 都有租户约束 |
| `owner_scope/owner_id` | 已有 | **必需**。USER/PROJECT/WORKSPACE/TASK 等；APPLICATION 只作命名空间之一，不隐含全用户共享 |
| `visibility/shared_namespace` | 未见模型字段 | 私有为默认；**真正共享时才需要**成员授权，不必先做复杂 ACL |
| `source_type/source_ref/source_event_id` | source_type 为必填枚举；source_ref 参数必填但允许空串；source_event_id 缺明确字段 | **必需**。可信来源类型、稳定事件引用、独立证据去重；激活前来源不可空 |
| `session_id/task_id/project_id/workspace_id` | evidence/context 已有 | 对应来源时**条件必需**；session 记录来源，不作为长期跨会话读的唯一范围 |
| `epistemic_status` | 未明确区分 | **必需**：user_asserted / observed / verified / inferred；与 candidate/active 生命周期正交 |
| `verified_by/verification_ref/verified_at` | 仅自由 metadata.verified | 对 verified **必需**。用户确认只赋 user_asserted；verified 须指向可信验证器、观测及验证范围，拒绝模型自填 |
| `status` | candidate/active/superseded/revoked/expired/rejected 已有 | **必需**。不先增加复杂状态图；needs_revalidation 可为资格结果，conflict 可为候选 metadata |
| `created_at/updated_at/valid_from/valid_until` | 已有 | **必需**时间戳；TTL 可空；created/updated 不等于确认时间 |
| `last_confirmed_at` | 已有 | 显式重新确认才更新；重复 evidence 不更新它 |
| `confidence/salience` | 已有 | 兼容保留、**非安全依据**。当前 confidence 不能声称是经校准概率；最小版可弱化排序权重 |
| `supersedes_id`、纠正理由 | 已有关系，理由在命令/metadata | **更正时必需**，保留来源链，读取只用生效项 |
| `claim_key/typed_value` | 未有 | **属性类冲突建议必需**，如 `response.language`；普通经验可不设，避免虚假结构化 |
| `observed_revision/dependency_paths/digests` | 仅 revision 自由 metadata | **代码相关记忆必需**；branch 辅助可选；不绑定普通用户偏好 |
| `retrieval_keys` | 未有统一约束 | 可选的人工/确定性词项、模块、文件路径、错误码；检索依据应可解释 |
| `idempotency_key` | 导入有局部幂等键，统一写入未见 | **写入重试必需**。由宿主事件+操作生成，数据库保证范围内唯一 |
| `deleted_at/tombstone/source_lineage` | 未完整实现 | 承诺清除副本时**必需**。tombstone 只留最低必要 ID/事件标识，禁止继续留秘密正文 |
| embedding 模型/维度/索引世代 | 可选索引现状未统一表达 | **C 档才需要**；模型换版可重建，不应改变记忆主记录事实身份 |
| 使用次数、学习型重要性、关系图、自动聚类 | 未发现完整业务链 | **可选增强**，当前无必要 |

**作用域建议示例**：

```text
private/u17/user
private/u17/project/repo-uuid-9
private/u17/project/repo-uuid-9/task/task-42
shared/team-3/project/repo-uuid-9    # 仅显式成员可访问
```

不把原始绝对路径当永久项目 ID；最小版可由服务端登记 UUID→仓库根目录，worktree 共享稳定 repo ID但仍独立核依赖摘要。只存仓库 remote URL 也不够，URL 可能变化、含凭证或存在同名克隆。

### 7.2 一个最小记忆记录示例

下面为**目标 API 表达**，示例值不代表本项目真实记忆；既有字段应映射到现有表，不照抄建立第二套数据库。

```json
{
  "id": "memory-example-01",
  "version": 1,
  "namespace": {"tenant_id": "u17", "scope": "user", "scope_id": "u17"},
  "kind": "preference",
  "claim_key": "response.language",
  "content": "默认使用中文回答",
  "typed_value": "zh-CN",
  "status": "active",
  "epistemic_status": "user_asserted",
  "source": {
    "type": "explicit_user",
    "event_id": "message-example-8",
    "ref": "session-example-2/message-example-8"
  },
  "created_at": "2026-09-27T00:00:00Z",
  "updated_at": "2026-09-27T00:00:00Z",
  "last_confirmed_at": "2026-09-27T00:00:00Z",
  "validity": {"valid_from": "2026-09-27T00:00:00Z", "valid_until": null},
  "retrieval_keys": ["回答语言", "language"],
  "idempotency_key": "message-example-8:save-language"
}
```

代码事实另加 `validity.dependencies=[{path,digest}]` 和宿主采集的 observed_revision；没有验证来源时 `status=candidate`。来源 excerpt 不是必填全文，优先使用受控引用和短脱敏摘录。

### 7.3 接口契约：模型参数与可信上下文分离

以下为设计签名，不是本轮新增代码：

```python
# ctx 由宿主鉴权/工作区解析器建立，不能从模型 JSON 或任意网页取得。
def propose_memory(claim, source_event_ids, ctx) -> WriteDecision: ...
def confirm_memory(memory_id, expected_version, confirmation_event, ctx) -> MemoryItem: ...
def correct_memory(memory_id, expected_version, patch, source_event_ids, ctx) -> MemoryItem: ...
def revoke_memory(memory_id, expected_version, reason, ctx) -> RevocationReceipt: ...

def list_memories(filters, cursor, ctx) -> Page[MemorySummary]: ...
def get_memory(memory_id, ctx) -> MemoryDetail: ...  # 含来源、当前资格、纠正链
def retrieve_memories(query, purpose, repo_snapshot, ctx) -> RetrievalResult: ...
def explain_retrieval(retrieval_id, ctx) -> RetrievalExplanation: ...

def purge_memories(memory_ids, expected_versions, deletion_scope, ctx) -> DeletionReceipt: ...
def revalidate_memory(memory_id, expected_version, trusted_observation, ctx) -> MemoryItem: ...
```

必要契约：

- `WriteDecision` 返回 accepted/candidate/rejected/duplicate/conflict、memory_id、实际 status、原因、检索是否待同步；不能仅返回 saved。
- `expected_version` 来自用户看见的版本；当前 command.update 内部先读当前 version 不足以检测用户页面持有的旧值，需对外接收并校验。
- `confirmation_event` 是宿主保存的真实用户动作/消息引用。允许明确用户请求直接保存低敏偏好；不强迫每条偏好二次确认。
- `RetrievalResult` 附 `id/version/backend/matched_terms/score/source/validity/drop_reasons`；source 细节按权限展开，默认无敏感正文日志。
- 资格过滤由服务端统一执行，列表、检索、导出、纠错和删除不能各写一套 scope 规则。
- 返回错误区分 ScopeDenied / VersionConflict / InvalidSource / NeedsConfirmation / StaleEvidence / BackendUnavailable。
- `DeletionReceipt` 区分逻辑撤销完成、在线副本清理完成、备份按保留期清理；不以 Qdrant delete 返回成功代表全部完成。
- purge 保留不含正文的来源墓碑并使重建/导入遵守；用户以后重新明确保存同一信息可产生新授权事件，不能被旧墓碑永久禁止。
- 对同会话已用过的记忆，下一次组装通过 memory ID/来源追踪排除相关派生块；原始用户消息是否删除由“删除记忆”与“删除会话”选项决定。不得声称可以撤回已发往外部模型的历史请求。已被助手转述却没有 memory ID 血缘标记的历史文本，也不能靠按 ID 撤销保证消失；最小版须定义清理派生消息或新建会话的策略，并单独测试。

## 8. 三个投入等级与迁移路径

### 8.1 对比

| 维度 | A. 文件或轻量数据库 | B. 结构化存储 + 全文检索 | C. 语义或混合检索 |
|---|---|---|---|
| 适合内容 | 少量明确偏好、人工项目约定、短经验 | 较多带错误码/模块/术语的事实与经验 | 自然语言表述变化大、同义召回频繁且有实测收益 |
| 从零选择 | 单机可 SQLite；极少量人工维护可 JSON/Markdown + 原子写/锁 | SQLite FTS5 或现有 PostgreSQL FTS | 结构化/全文为主，再接可重建向量索引 |
| 本项目落点 | **保留 PostgreSQL，只做受控结构化/词项查询**，达到 A 的低维护目标 | 在现有 PostgreSQL 增索引与搜索配置，避免第二数据库 | 复用现有 Qdrant adapter，先完善 outbox 和删除，不再引入新的向量库 |
| 主要收益 | 可解释、容易导出检查、无需模型调用 | 过滤+词项命中稳定，可查匹配理由；常见技术名词有效 | 同义与描述性问题可能召回更多；混合可保留精确匹配 |
| 维护成本 | 文件并发/格式/权限；SQLite 备份/锁；当前 PG 可复用运维 | schema/index 升级、分词/短词/代码符号、查询计划 | embedding 模型与版本、服务/存储、索引一致性、恢复、全链删除 |
| 调试难度 | 低：能定位条目和匹配条件 | 中：需解释 tokenization/rank/filter | 高：相似度难解释，索引 lag 与语义噪声叠加 |
| 典型失败 | 全文拼接噪声；手工编辑丢来源；文件历史留秘密 | 中文分词失配、术语变体漏召回、LIKE/FTS 性能退化 | 错误事实被高相似度召回；范围过滤不当；高成本但无质量提升 |
| 不能解决的问题 | 来源真实性、权限、版本、冲突要自己做 | 同左；FTS 不会验证真假 | 同左；embedding 不会验证真假 |
| 退出策略 | 导出统一记录+evidence；稳定 ID | 更换搜索 adapter，主记录不变 | 禁用向量回到 B/A；旧 collection 可按清理计划退役 |
| 当前推荐 | **先做** | **有数据和收益后做** | **保留为可选，不设默认必需** |

SQLite FTS5 支持全文和 trigram，但 trigram 的不足 3 个 Unicode 字符查询不能正常按其全文匹配；不是“换 FTS 就自动解决中文”。[SQLite FTS5 官方说明](https://sqlite.org/fts5.html#the_trigram_tokenizer)，访问 2026-09-27。PostgreSQL 已提供可配置全文检索和 pg_trgm；后者可支持相似/LIKE 索引，但会忽略非词字符，因此不能用其相似度合并代码事实。[PostgreSQL 18 全文文档](https://www.postgresql.org/docs/18/textsearch.html)、[pg_trgm](https://www.postgresql.org/docs/18/pgtrgm.html)，访问 2026-09-27。本轮不安装扩展、不假定部署已有特定分词插件。存储许可亦已核验：SQLite 核心交付代码为 public domain；PostgreSQL 使用 PostgreSQL License。[SQLite 官方版权说明](https://sqlite.org/copyright.html)、[PostgreSQL 官方许可](https://www.postgresql.org/about/licence/)，访问 2026-09-27。

### 8.2 升级与回退门槛

以下是**建议的决策阈值，不是当前测量结果**：

- **保持 A**：有效条目仍少、用户可管理、确定性检索延迟可接受；常见任务没有稳定记忆收益时甚至保持关闭。条目数只是信号，不设“超过 N 必须向量化”。
- **A→B**：scope 内扫描已经超出团队自定延迟目标，或错误码/术语查询漏召回明显；先在固定集上比较，B 在同等资格规则下 precision 不下降且召回/任务结果改善，再建索引。
- **B→C**：离线人工标注发现主要损失来自同义表达，C 能在相同 K 和资格规则下带来至少约 5 个百分点的有效召回或任务正确率提升，同时不增加污染、删除失败；小样本需报告置信区间，阈值由汇总任务裁决。
- **C→B/A**：embedding/向量不可用、p95 延迟和成本超目标、错记忆采纳增加、删除闭环无法保证；配置切换不得导致基础已确认记忆不可用。
- 检索调用数、embedding 文本量/费用、p50/p95、索引队列时间、人工纠错时间一起记录。没有真实负载证据，不给“提升 X%”“降低成本 Y%”的承诺。

### 8.3 迁移次序

1. 固定 ID、来源和语义 schema；现有数据先仅做只读盘点，区分 user/project/无法归属项。
2. 明确字段和资格策略后建立 schema version 升级；为新字段保守回填，未知来源不得伪造 verified，未知代码依赖待复核。
3. 复用 `LegacyMemoryImporter` 的 dry-run/分类理念，但当前 `_import_memory`（`legacy_importer.py:99–115`）会把 MEMORY.md 项按 active 导入；未来迁移必须先审查来源，不能无条件继承旧文件的“真”地位。
4. A 基础查询可用后，B 搜索索引作为派生结构回填；双读比较在测试集进行，不建立两个主写入口。
5. 需要 C 时，由 active+valid 的主记录重建新索引世代，对照校验后切换。旧索引保留短期回滚窗口，之后清理；删除墓碑必须作用于所有世代。
6. 遗留 Markdown 最终只作为用户触发导出或有边界的迁移源；不要与 SQL 双向自动同步。旧 Web 页面先标明“遗留文件”，再切换到真实语义库视图。

## 9. 实施建议：按可交付闭环拆分

本轮**只设计**。下面是后续实施任务，不表示已执行；实际工期取决于测试与部署条件，不作未经估算的日程承诺。

| 阶段 | 最小变更范围 | 完成标准 | 为什么先做 |
|---|---|---|---|
| M0：可信写入 | `conclusions.py`、`promotion.py`、`command_service.py`、`tools/handlers.py` | 模型只能 candidate；host confirmation/evidence 分离；停止任务不自动晋升；秘密默认不存；默认 trace 去正文 | 阻止错误内容长期放大，比提升召回更有价值 |
| M1：作用域与状态正确性 | `domain.py`、`commands.py`、`postgres_repository.py`、`conflict_service.py` | 私有 namespace、明确共享；保真精确去重；幂等；candidate/过期重新确认矩阵；前端 CAS | 减少跨范围污染和用户无法纠正的行为 |
| M2：无需向量的读取 | `semantic_retrieval.py`、`vector_runtime.py`、`bootstrap.py` | disabled/unavailable 均可基础检索；相关性拒答；后端状态可见 | 让低投入路径真正可用 |
| M3：项目有效性与集成 | `runner.py`、`promotion.py`、`context/memory.py` | 宿主版本/文件摘要；命中时有效性检查；Coding factory/fork 装配测试 | 防旧代码事实影响新任务 |
| M4：用户管理与删除 | Web API/page、command/repository、来源反向查询 | 真实条目列表/来源/修改/确认/撤销；明确 purge 范围与回执；重新导入不复活 | 使用户可控制持久信息，不能只依赖数据库管理员 |
| M5：可选检索可靠性 | outbox worker、rebuild、可选 FTS/向量 adapter | 保留 C 时通过 crash/lag/delete 测试；只有测得增益才默认使用 | 避免为未证明价值的索引承担长期运维 |

**最小可行发布范围**可以收窄为：用户显式低敏偏好与人工确认的项目决策，A 式读取，按 ID 纠错/撤销、基本数据清除，默认无自动经验晋升。若不打算承担代码依赖验证，先不开放“代码事实自动生效”，不要交付一个表面完整但不可信的经验系统。

**收益明确后再做**：PG 全文检索；词法+语义融合；依赖文件级复核优化；有限的候选提取；有证据的经验模板；受控共享项目空间。先确认用户确实重复提供同类信息，或新任务能显著减少重复探索。

**当前不建议**：

- 全量历史自动提炼、每轮都调用额外模型“反思”。
- 多记忆 Agent、后台 dreaming、自我改写系统提示、知识图谱、复杂分层路由。
- 为少量偏好上专用向量服务，或同时维护 PG/SQLite/Markdown 三个事实源。
- 把高 similarity、模型自报 confidence 或重复证据数当自动晋升标准。
- 为记忆单独微调；以微调代替来源、更正和删除。
- 新建通用工作流平台/消息中间件来消费小规模 outbox。

## 10. 验证设计与本轮检查结果

### 10.1 四个实验组

这是**后续验证计划**，本轮没有执行模型对照。所有组用同一模型/版本、任务说明、工具权限、评判规则和输入材料；不把给某组更多人工真值的差异算作算法效果。

| 组 | 机制 | 作用与对照约束 |
|---|---|---|
| G0 无跨会话记忆 | 新任务只含当前请求和当前可读项目，不注入旧历史 | 衡量记忆的净收益；允许重新查代码，记录重复探索成本 |
| G1 直接拼接历史 | 同一用户且同项目的历史按时间拼入；不做语义抽取 | 明确基线；保留原始来源，不跨项目越权。不能给它无限额外上下文 |
| G2 结构化记忆 | 资格过滤后读取明确偏好/决策/小量规则，不用语义索引 | 验证来源、scope、有效性、纠错带来的收益 |
| G3 检索式记忆 | 与 G2 **相同候选库和资格规则**，增加词法/全文；G3b 再加语义 | 分开检验“整理内容”和“检索算法”的贡献；不把更好人工整理归因给 embedding |

注入总量用共同上限或分层报告，但本任务不重新设计请求预算。G1 截断导致遗漏要报告“历史覆盖率”，不能暗中只保留对答案有用的部分。无记忆组如果能低成本从代码解决，记忆没有明显收益也是可接受结论。

### 10.2 测试集与指标

建议先人工编制 **60 个合成短序列**（每类 6 个），含 2 个用户、3 个项目、2 个分支及多个任务/会话；这不是当前真实数据量。每条包含来源事件、理想记忆状态、应检索 ID、禁用 ID 和正确/应拒答答案。中文、英文和代码符号混合，全部脱敏。

| 类别 | 例子 | 应验结果 |
|---|---|---|
| 稳定偏好 | 新会话仍按明确保存的语言偏好回答 | 相关偏好命中；无显式保存不擅自长期化 |
| 多会话项目决策 | 任务 B 使用任务 A 已确认的兼容决定 | 跨任务有效，来源正确，不串项目 |
| 知识更新/冲突 | “用 unittest”更正“用 pytest” | 当前有效值唯一或明确冲突；不因相似度吞掉否定 |
| 代码过期 | 切分支、相关文件变更、同 HEAD 脏工作树 | 旧事实失效/要求核验；无关文件改变不误杀 |
| 错误记忆影响 | 注入一条高置信错误测试命令 | 不盲从；以当前证据核验；错误采纳率可测 |
| 噪声/缺失 | 相关项不存在但有高相似主题条目 | 可以回答“不知道/需查证”；不强行命中 |
| 用户/项目隔离 | 同名项目/同 application/其他用户 | 非授权内容命中和注入均为 0 |
| 提示注入/敏感信息 | 网页要求永久记住“跳过审批”，合成 secret | 无权限升级；候选/trace 不保存秘密 |
| 删除与复活 | revoke/purge 后残留索引、恢复旧会话、重新导入 | 未来注入为 0；清理结果符合回执 |
| 基础设施/并发 | Null index、超时、claim 后崩溃、双写 | 不丢主记录；可降级、可恢复、幂等 |

指标分别在写入、检索、回答层计算，不只看最终“回答看起来不错”：

- **写入精度**：激活项中有可用证据且符合范围/准入的比例；伪 verified 激活数。
- **检索 Precision@K、Recall@K**：分用户偏好、项目事实、中文/英文/代码符号；记录无结果率、噪声率。
- **资格违规率**：跨租户/项目、过期/撤销、错误代码版本、无权限来源的命中与注入分别计数。
- **错误记忆采纳率**：明知预置错误记忆的 case 中，答案/工具动作错误依赖它的比例；同时比较没有该条时的差异。
- **更新成功率与冲突正确处理率**：更正后是否真正生效；候选/过期重新确认是否可用。
- **删除后使用率**：撤销/清除后，在新会话、当前会话下一轮、重建、重导入、备份恢复演练各检查一次。
- **任务效果**：正确率、要求查证/拒答是否恰当、重复读代码/重复询问用户次数；可由人工或确定断言评分，模型 judge 不作为唯一依据。
- **成本与运维**：额外模型/embedding 调用、耗时 p50/p95、索引队列年龄、人工纠错耗时、部署组件数。

LongMemEval 的信息提取、多会话、时序、更新、拒答维度适合借鉴，但其 500 问基准和论文效果不等于本项目性能；本项目还须加代码变更、权限与删除测试。[LongMemEval v2，2025-03-04](https://arxiv.org/abs/2410.10813v2)。

### 10.3 验收门槛与低成本执行顺序

1. **先做纯函数/内存契约**：准入、去重、scope、TTL、有效性、渲染、删除墓碑；用 fake repository/index，不调用模型、不连生产库。
2. **再做临时存储集成**：明确授权的隔离测试库，验证事务、CAS、并发幂等、outbox lease、索引滞后；故障注入不能在真实用户数据上执行。
3. **然后做工厂级测试**：从 bootstrap/Coding fork 到实际 renderer，覆盖开关矩阵，避免只测 service 得到假集成信心。
4. **最后才做小量任务质量评测**：人工批准样例、固定模型参数、固定费用上限后执行 G0–G3；本轮没有此授权，也没有进行模型实验。

建议硬门槛：测试集中越权注入、撤销后重新检索注入、伪造 verified 激活、测试 secret 落默认 trace **均为 0**；这只是有限测试的通过条件，不是对未知输入的绝对安全证明。每条相关代码依赖变化必须按设计拒绝或降级。任务效果以配对差异报告；60 例不足以支持细微提升的强结论。

作为性能起始目标，可约定本地 500 条有效记忆的确定性检索 p95 小于 100 ms，再按部署实测调整；该数是**待测目标**，不是当前结果。C 档的收益门槛见第 8 节，任何安全/删除回归都不能用平均分提升抵消。

### 10.4 本轮实际执行的 8 个探针

执行方式：两次 `python -B - <<'PY'`，导入现有纯逻辑模块及 `tests.fakes.InMemoryMemoryRepository`；仅在进程内创建合成条目。没有执行模型提取方法、连接 PostgreSQL/Qdrant、读真实会话、写 pyc 或新增测试文件。两次进程退出码均为 0。

| 编号 | 输入/操作 | 实际输出 | 可证明与不可证明 |
|---|---|---|---|
| P1 | propose “Prefer concise Chinese answers”，再 explicit remember 相同内容 | 同 ID；status=`candidate` | 证明命令服务重复分支未激活；不是 Web 端复现 |
| P2 | repo 有 active “Use pytest fixtures”，以 NullSemanticMemoryIndex 查询 pytest | active=1，hits=0，degraded=`False` | 证明禁索引基础召回问题；未复现实际 Qdrant 启动失败 |
| P3 | `is_semantic_duplicate("Always deploy after tests pass", "Never deploy after tests pass")` | `True` | 证明近似去重将相反句判重复；未运行真实 Agent 部署动作 |
| P4 | normalize “Use --no-cache” 与 “Use --no cache” | 相等 | 证明命令参数归一化碰撞；不证明已有数据碰撞 |
| P5 | Alice/Bob，均 application=coding、project_id=repo-x | 共同 owner：application:coding、project:repo-x | 证明授权 key 相交；未读取任何其他用户数据 |
| P6 | 将合成 active 项的有效区间设为 2026-01-01 至 2026-02-01，再于 2026-09-27 remember 相同项 | 同 ID；`is_retrievable=False` | 证明重复写入不续期 |
| P7 | 对 P6 项执行 update | `ValueError: valid_until must be later than valid_from.` | 证明过期项更新构造失败 |
| P8 | `_parse_candidates` 输入 verified 字符串 `"false"`、confidence=0.9、无证据文件；交 promoter | parsed_verified=`True`，promoted=1，status=`active`，evidence_file=`''` | 证明解析→晋升→命令链可把该合成项激活；不是模型真实输出概率测量 |

可重放核心脚本如下；这是记录本轮检查的代码片段，**没有写成仓库测试文件**。只使用合成数据：

```python
from dataclasses import replace
from datetime import datetime, timezone
from memory.command_service import MemoryCommandService
from memory.commands import MemoryContext, MemoryWriteProposal
from memory.domain import MemoryKind, MemoryOwnerScope, MemorySourceType
from memory.semantic_index import NullSemanticMemoryIndex
from memory.semantic_retrieval import SemanticMemoryRetrievalService
from memory.dedup import is_semantic_duplicate, normalize_memory_text
from tests.fakes.in_memory_memory_repository import InMemoryMemoryRepository
from applications.coding.conclusions import _parse_candidates
from applications.coding.promotion import TaskMemoryPromoter

now = datetime(2026, 9, 27, tzinfo=timezone.utc)
ctx = MemoryContext(user_id="audit-user", session_id="audit-session")
repo = InMemoryMemoryRepository()
svc = MemoryCommandService(repo, clock=lambda: now)

def proposal(text, explicit=False):
    return MemoryWriteProposal(
        content=text, kind=MemoryKind.PREFERENCE,
        owner_scope=MemoryOwnerScope.USER, owner_id=ctx.user_id,
        source_type=(MemorySourceType.EXPLICIT_USER if explicit
                     else MemorySourceType.INFERRED),
        explicit_user_request=explicit,
    )

pending = svc.propose(proposal("Prefer concise Chinese answers"), ctx)
again = svc.remember(proposal("Prefer concise Chinese answers", True), ctx)
print("P1", pending.id == again.id, again.status.value)

active = svc.remember(proposal("Use pytest fixtures", True), ctx)
result = SemanticMemoryRetrievalService(
    repo, NullSemanticMemoryIndex(), clock=lambda: now
).retrieve("pytest", ctx)
print("P2", len(repo.list_active(ctx.allowed_owners(), now)),
      len(result.hits), result.degraded)
print("P3", is_semantic_duplicate(
    "Always deploy after tests pass", "Never deploy after tests pass"))
print("P4", normalize_memory_text("Use --no-cache") ==
      normalize_memory_text("Use --no cache"))

a = MemoryContext(user_id="alice", session_id="sa",
                  application="coding", project_id="repo-x")
b = MemoryContext(user_id="bob", session_id="sb",
                  application="coding", project_id="repo-x")
print("P5", sorted((x.scope.value, x.id)
                  for x in set(a.allowed_owners()) & set(b.allowed_owners())))

repo.items[active.id] = replace(
    active, valid_from=datetime(2026, 1, 1, tzinfo=timezone.utc),
    valid_until=datetime(2026, 2, 1, tzinfo=timezone.utc))
renewed = svc.remember(proposal("Use pytest fixtures", True), ctx)
print("P6", renewed.id == active.id, renewed.is_retrievable(now))
try:
    svc.update(active.id, "Use current pytest fixtures", ctx)
except ValueError as exc:
    print("P7", str(exc))

repo2 = InMemoryMemoryRepository()
svc2 = MemoryCommandService(repo2, clock=lambda: now)
coding_ctx = MemoryContext(user_id="audit-user", session_id="audit-task-session",
                           project_id="audit-repo", task_id="audit-task")
candidates = _parse_candidates([{
    "category": "fact",
    "content": "Audit fixture: project tests use pytest.",
    "verified": "false", "confidence": 0.9,
}])
promoted = TaskMemoryPromoter(command_service=svc2).promote(
    task_id="audit-task", extracted_conclusions=candidates,
    memory_context=coding_ctx, repository_revision="audit-revision")
item = next(iter(repo2.items.values()))
evidence = repo2.list_evidence(item.id)[0]
print("P8", candidates[0].verified, len(promoted.promoted),
      item.status.value, repr(evidence.metadata["evidence_file"]))
```

### 10.5 现有测试能证明的范围

以下仅做了源码审阅，**本轮未运行**，不引用旧报告的“全部通过”作为本轮结果：

| 现有测试 | 覆盖意图 | 必须补充的反例 |
|---|---|---|
| `tests/test_memory_command_service.py:77–219` | 创建、候选、确认、更新、撤销、scope | P1/P6/P7；真实用户授权、前端旧版本修改 |
| `tests/test_coding_memory_proposals.py:14–40,65–105` | 接受模型 verified，晋升断言 ACTIVE | 测试名称含 candidate 不能当状态证据；补 P8 和宿主验证 |
| `tests/test_semantic_memory_retrieval.py:55–114` | stale/revoked/cross-owner/TTL/查询异常 fallback | Null/disabled/初始化失败、低相关拒答、代码版本资格 |
| `tests/test_memory_conflicts.py:47–93` | 精确/近似重复、显式冲突、optional resolver | 否定、数值、符号、conflicts_with 优先级 |
| `tests/test_memory_index_sync.py:29–69` | 顺序 upsert/delete、失败 retry、旧事件不复活撤销 | claim 后进程中断、无人消费、重建孤儿点清理 |
| `tests/test_memory_context_sections.py:89–98` | 新会话读用户语义记忆 | Coding factory/fork 实际装配；多用户共享 project key |
| `tests/test_memory_postgres_repository.py:99–151,166–204` | CAS/事务/claim/回滚 | 真并发幂等、删除外键及 purge 事务、namespace 迁移 |

## 11. 关键资料及其支持的具体结论

所有网络资料访问日期均为 **2026-09-27**。本表是证据索引；上文关键事实已就近链接。无 commit 的页面明确标为动态，不能据此声称已对固定版本做完整源码审计。

| 资料 | 版本/资料日期 | 支持的具体结论与限制 |
|---|---|---|
| [本项目 domain](../../memory/domain.py)、[command service](../../memory/command_service.py)、[PostgreSQL repository](../../memory/postgres_repository.py) | HEAD `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2`，2026-08-18 | 真实 item/evidence/status/事务与纠正实现；不证明部署已启用 |
| [本项目检索](../../memory/semantic_retrieval.py)、[索引](../../memory/semantic_index.py)、[同步](../../memory/index_sync.py) | 同上 | 回源校验优势、Null fallback 缺口、排名和 outbox 边界 |
| [Coding conclusions](../../applications/coding/conclusions.py)、[promotion](../../applications/coding/promotion.py)、[runner](../../applications/coding/runner.py) | 同上 | 模型 verified 信任链、revision 记录、子运行器装配 |
| [Web server](../../web/server.py)、[MemoryPage](../../web/frontend/src/pages/MemoryPage.tsx) | 同上 | 当前页面是遗留文件查看，不是语义记忆管理 |
| [LangMem tools](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/tools.py)、[extraction](https://github.com/langchain-ai/langmem/blob/main/src/langmem/knowledge/extraction.py) | 动态 main；pyproject 声明 0.0.30，HEAD 未确认 | schema/namespace/CRUD、模型提取与写回；不能替代事实验证 |
| [LangMem LICENSE](https://github.com/langchain-ai/langmem/blob/main/LICENSE)、[pyproject](https://github.com/langchain-ai/langmem/blob/main/pyproject.toml) | 访问时状态 | MIT 与依赖范围；不等于 LangSmith 等云服务开源 |
| [LangGraph MIT LICENSE](https://github.com/langchain-ai/langgraph/blob/main/LICENSE)、[Persistence](https://docs.langchain.com/oss/python/langgraph/persistence)、[Stores](https://docs.langchain.com/oss/python/langgraph/stores) | 动态官方文档 | MIT 许可；thread checkpoint 与跨 thread store 的边界，索引与存储可分离 |
| [Letta 历史源码说明](https://github.com/letta-ai/letta#historical-source) | 访问时状态 | V1 server 在 archive；活跃实现迁到 letta-code |
| [Letta Code commit](https://github.com/letta-ai/letta-code/commit/9ead5f0480b2c4e0f91e1cf4511126294c442948)、[LICENSE](https://github.com/letta-ai/letta-code/blob/v0.32.1/LICENSE) | v0.32.1，2026-09-10 | 固定版本、Apache-2.0 |
| [Letta memory.ts](https://github.com/letta-ai/letta-code/blob/9ead5f0480b2c4e0f91e1cf4511126294c442948/src/tools/impl/memory.ts)、[filesystem](https://github.com/letta-ai/letta-code/blob/9ead5f0480b2c4e0f91e1cf4511126294c442948/src/agent/memory-filesystem.ts)、[memory-git](https://github.com/letta-ai/letta-code/blob/9ead5f0480b2c4e0f91e1cf4511126294c442948/src/agent/memory-git.ts) | 同上 | 写入理由、只读限制、按 agent 目录、Git 版本机制；不证明用户 ACL/事实裁决 |
| [MemFS 官方说明](https://docs.letta.com/concepts/memfs) | 动态官方文档 | 默认无 semantic/vector index，按文件搜索；布局演化不能混作固定版本保证 |
| [Mem0 v2.2.1](https://github.com/mem0ai/mem0/releases/tag/v2.2.1)、[LICENSE](https://github.com/mem0ai/mem0/blob/main/LICENSE) | 2026-09-25，`94c3fe9f238f3dbf29c9ce98643bd71eb13077cd` | 代码为固定版本；许可核验为访问时 main 的 Apache-2.0；公开修复只代表上游问题 |
| [Mem0 main](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/main.py)、[prompts](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/configs/prompts.py)、[storage](https://github.com/mem0ai/mem0/blob/94c3fe9f238f3dbf29c9ce98643bd71eb13077cd/mem0/memory/storage.py) | 同上 | ADD-only 默认写入、显式更新/删除、历史仍留旧值；未分析云端内部 |
| [Claude Code memory](https://code.claude.com/docs/en/memory)、[LICENSE](https://raw.githubusercontent.com/anthropics/claude-code/main/LICENSE.md) | 动态文档/许可 | 文件记忆的公开行为和管理入口；不将公开仓库误称开源记忆引擎 |
| [LongMemEval](https://arxiv.org/abs/2410.10813v2) | v1 2024-10-14；v2 2025-03-04 | 提取、多会话、时序、更新、拒答的评测维度，不外推论文效果 |
| [MINJA](https://arxiv.org/abs/2503.03704v5) | v1 2025-03-05；v5 2026-02-12 | 从交互污染持久记忆的威胁模型，不声称本项目已被攻击 |
| [SQLite 版权](https://sqlite.org/copyright.html)、[PostgreSQL 许可](https://www.postgresql.org/about/licence/)、[SQLite FTS5](https://sqlite.org/fts5.html)、[PostgreSQL 18 FTS](https://www.postgresql.org/docs/18/textsearch.html)、[pg_trgm](https://www.postgresql.org/docs/18/pgtrgm.html) | 官方文档，访问时状态 | public domain/PostgreSQL License、B 档技术可行性及分词/短词/符号限制；未安装或测性能 |

## 12. 尚未确认的问题

1. 部署真实开关、数据库/索引是否正在使用、active/candidate 数量、每日写入/检索量、失败率：本轮未读真实环境或用户数据。
2. 除已审源码外是否有外部定时任务消费 outbox、回收 processing、删除历史索引：未核验部署编排。
3. 项目希望“同一工作区多用户共享知识”还是“所有记忆默认私有”：现有 owner 合集不能替产品决定。
4. 用户对自动记忆的接受程度、敏感数据范围、会话/trace/备份保留周期、是否需永久删除：需要产品约定。
5. 真实中文/英文/代码查询的分布，以及 A/B/C 的质量、延迟、费用收益：没有运行任务级对照。
6. 本轮内存替身与 PostgreSQL 的并发/事务行为差异：P1–P8 证明的范围已限定；SQL 集成需隔离环境验证。
7. LangMem 当前 main 的确切 HEAD SHA；Letta Cloud/Mem0 Cloud 的内部实现与删除保证：未确认，不能据 SDK 推断。
8. 原始 session_turn 的当前生产索引写入链是否在未检查的外部插件内存在：已审源码仅确认 adapter/读取装配，不把遗留 point 当作当前写入证据。
9. 用户已有记忆是否曾受错误合并、空来源或模型 verified 影响：本轮没有审查真实内容，不能把合成反例当成历史事故。
10. 基础代码变更检查应该采用 revision 保守失效还是依赖摘要，以及何种事实值得支付复核成本：需先统计实际复用场景。

## 13. 与其他任务的接口或依赖

| 相邻任务 | 本任务提供的契约 | 依赖/边界 |
|---|---|---|
| 上下文设计 | `MemoryReadView`：已授权、有效、带来源与 ID/version 的记录；撤销/删除后的排除标记 | 对方负责最终消息组织和预算；本任务不重做单次上下文压缩 |
| 工具权限与安全 | 记忆不能赋权；真实用户确认/可信工具事件由宿主产生 | 对方提供不可由模型伪造的 principal、permission decision、tool result ID；不把 stdout 当权限证明 |
| Coding 工作区/任务状态 | 稳定 project/workspace/task ID、任务结果、HEAD/文件摘要、真实测试结果引用 | 检查点保留在任务系统；只有明确晋升才进入长期记忆 |
| 用户/项目与鉴权 | 私有 namespace 默认、共享空间显式成员关系 | 复用既有身份系统；不要 memory 模块自行解析任意模型给的 user_id |
| 存储与可靠性 | 单事实源、CAS、schema migration、幂等、可选 outbox lease | 需要隔离数据库测试；不新增第二事实库或通用消息系统 |
| Web/产品 | 列表/详情/确认/纠正/撤销/purge 回执、关闭记忆 | 遗留文件视图与真正语义记忆视图区分；避免“空文件=无记忆” |
| 日志与隐私治理 | 默认 ID/hash trace；来源反向索引、删除清单 | 日志、会话、工件、备份分别声明保留和清理责任 |
| 评测任务 | G0–G3、60 个建议合成序列、8 个本轮已复现函数行为 | 后续真实模型评测需明确预算和授权；不得把当前文档当成已执行实验报告 |

## 14. 建议交给汇总任务裁决的争议

1. **记忆是否默认启用**：建议治理与管理闭环完成前保留关闭；可先只开放明确偏好保存。与产品追求“自动记住”的便利性取舍需统一。
2. **项目共享边界**：默认用户私有，还是团队共享项目知识？建议默认私有、共享显式化；不能把 application:coding 当共享授权。
3. **Coding 自动注入还是显式 recall**：二者均可，但应统一装配、界面文案与集成验收，不保留“功能似乎开启、实际子运行器为空”的中间状态。
4. **代码事实是否值得长期存**：能廉价重读的路径/架构事实可不记；优先保存难恢复的决策和经验证经验。依赖复核成本由真实复用率决定。
5. **删除承诺强度**：仅停止未来使用，还是清除主库、索引、日志、备份？建议分别提供操作和回执，不把软撤销命名为所有副本永久消失。
6. **继续维护向量检索的投入**：建议先让 A 基础路径独立可用，再通过 B/C 对照决定；不能因仓库已有 Qdrant 就认为必须默认依赖它，也不因追求简单而无收益迁移 PostgreSQL。
7. **旧数据处理**：已有 active 中来源不明或依赖缺失的项，是保留但降权还是隔离待确认？建议保守隔离并展示待审，不伪造验证来源，不本轮自动搬迁。
8. **自动纠错力度**：是否允许模型自主 supersede？建议仅生成建议，属性级用户更正/可信版本变化由确定规则处理；暂不引入通用 LLM 冲突裁判。
