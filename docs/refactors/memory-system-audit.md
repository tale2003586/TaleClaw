# TaleClaw 长期记忆系统现状审计

> 审计日期：2026-07-26；代码基线：`refactor/long-term-memory-system` 分支。本文只描述重构前链路，不把目标设计误写为现状。

## 1. 审计范围与方法

本次覆盖 `memory/`、`runtime/context/`、`runtime/bootstrap.py`、`runtime/sessions/`、`tools/`、`applications/coding/`、PostgreSQL、Qdrant 与 Trace。静态审计使用 `rg`、文件阅读和现有测试；规模统计只读取文件长度、数据库行数与向量 payload 分类，不输出消息或记忆正文。

当前仓库没有 `applications/minecraft/`。本次只定义 Application-local State 边界，不创建虚构的 Minecraft adapter。

## 2. 数据分类与当前事实来源

| 数据 | 当前载体 | 当前实际真源 | 重复或漂移 |
|---|---|---|---|
| Session History | PostgreSQL session/message store | Session Store | 同一 turn 还可能进入 HISTORY、RECENT_CONTEXT、memory_archive、Qdrant |
| Episodic History | Qdrant `session_turn`、`memory_archive`、HISTORY | 没有统一真源 | 查询边界和保留策略不一致 |
| Long-term Semantic Memory | 用户目录 `MEMORY.md` | Markdown | 文件会作为整份 `memory_file` 再索引 |
| Candidate Memory | `PENDING.json` 与 `PENDING.md` | 两者都被写入 | JSON 与 Markdown 可独立漂移 |
| Working Memory | runtime Working Memory、Coding Context State | 各自模块 | 不应并入长期记忆 |
| Application State | Coding task-local memory/context | Coding Application | promotion 会把结论写到全局 PENDING.md |

## 3. 普通会话 after-turn 写入链路

`runtime/bootstrap.py` 装配 `MemoryLifecycle`。每轮完成后，`MemoryLifecycle.after_turn()` 会：

1. 从最后一条用户消息提取显式“记住”文本，或调用 `MemoryProcessingDevice` 生成候选。
2. 显式文本直接 `MemoryStore.append("memory", ...)`；候选写入 `PENDING.json`，并同步追加 `PENDING.md`。
3. 把用户原文与助手摘要追加到 `HISTORY.md`。
4. 把本轮完整消息作为 `session_turn` 写入 Qdrant。
5. 把 SELF、MEMORY、NOW、PENDING、HISTORY 五份文件分别以整文件 `memory_file` point 写入 Qdrant。
6. 重写 `RECENT_CONTEXT.md` 与 `RECENT_CONTEXT.json`；淘汰窗口写入 PostgreSQL `memory_archive`。

因此一次会话 turn 可能同时出现在 Session Store、HISTORY、RECENT_CONTEXT、memory_archive 和 Qdrant。

## 4. 显式记忆工具链路

`tools.handlers.run_memorize()` 当前通过 Session 解析 `MemoryStore`，然后直接调用 `append(section, content)`。普通会话写用户级 Markdown；Coding 会话写 task-local Markdown。该入口没有统一的 owner/kind/status/source/evidence/version 模型，也不产生与事实状态同事务的索引事件。

`section` 参数还允许调用方选择 SELF、NOW、PENDING、HISTORY 等文件，业务语义与物理文件耦合。

## 5. 普通候选与晋升链路

`MemoryProcessingDevice` 先在 history vector index 中找相似历史，再由 extractor 生成候选。`MemoryStore.upsert_candidate()` 更新 `PENDING.json`，首次创建时还追加 `PENDING.md`。`MemoryLifecycle._promote_ready_candidates()` 按 confidence 或 evidence count 把内容追加到 `MEMORY.md`。

问题：

- evidence count 不能保证来自独立 Session。
- promotion 直接写 Markdown，没有作用域、版本、冲突和撤销事务。
- PENDING.json 的结构化状态与 PENDING.md 的文本副本会漂移。

## 6. Coding Conclusion 链路

`TaskConclusionExtractor` 从 coding task 提取 category/content/evidence/confidence。`TaskMemoryPromoter` 过滤噪音后，把结论通过 `global_memory.append_pending()` 写入全局 `PENDING.md`。

这条链路不复用普通候选的 PENDING.json 模型，也没有可靠保存 workspace、repository revision、project、task 和 evidence location。项目事实可能失去原作用域，task-local state 与长期语义候选边界不清晰。

## 7. Markdown recall 与 Context 注入

`ContextMemoryService.build_memory_block()` 有查询时调用 `MemoryStore.recall(query)`。该方法同时扫描 SELF、MEMORY、NOW、PENDING 和 HISTORY，并按文本词项排序后输出统一 `<memory>` 区块。

结果是候选、历史流水与稳定事实共享同一种展示语义；过去发生过的对话可能被模型当成当前有效事实。无查询时虽然默认只读 SELF/MEMORY/NOW，但仍没有状态、有效期和版本过滤。

## 8. Qdrant 历史检索与跨 Session 泄漏

`history_vector_scope_for_session()` 对普通会话返回 `user:<user_id>`。`ContextRetrievalService.retrieve_history()` 只把这一单一 scope 传给 vector index，Qdrant filter 也只匹配 payload 的 `scope`。

因此同一用户 Session A 与 Session B 使用相同过滤条件；B 可以召回 A 的原始 `session_turn`。这是新 Session 出现历史消息的直接原因。Coding 会话使用 `task:<task_id>`，边界相对更窄。

## 9. PostgreSQL 使用现状

PostgreSQL 已保存 sessions/messages，并通过 `MemoryArchiveStore` 保存 recent window 淘汰的摘要 turn。各 Store 使用幂等 `_init_schema()`，当前没有 Alembic。

长期 semantic memory 没有 PostgreSQL 表；数据库也没有统一 evidence、版本、状态或 index outbox。`memory_archive` 是历史摘要归档，不应作为 semantic memory 来源。

## 10. Qdrant 使用现状

`QdrantMemoryVectorIndex` 的 point payload 是 `MemoryRecord` 全量序列化，查询仅按 `scope` 过滤。collection 同时承载 `session_turn` 和 `memory_file`，长期语义与历史事件未分离。

整文件 point ID 按 scope 与文件名固定，文件变化会覆盖该 point；但文件被清空或停写时会跳过 upsert，旧 point 没有可靠删除链路。撤销或清空 Markdown 不能保证旧向量同步失效。

## 11. Trace 与评测缺口

现有 lifecycle 会发出 candidate、history vector 和 file vector 等事件，但没有统一的 item ID、owner、kind、version、previous version、index status 和 drop reason。当前 summary 也无法完整计算晋升率、冲突率、陈旧命中率、跨作用域泄漏率和 Context token 占比。

## 12. 当前数据规模快照

2026-07-26 在当前 checkout 做只读统计：

| 载体 | 计数 |
|---|---:|
| `memory/SELF.md` | 0 行 / 0 字节 |
| `memory/MEMORY.md` | 0 行 / 0 字节 |
| `memory/NOW.md` | 0 行 / 0 字节 |
| `memory/PENDING.md` | 5 行 / 147 字节 / 1 个 bullet |
| `memory/PENDING.json` | 1 行 / 3 字节；当前内容不是候选对象结构 |
| `memory/HISTORY.md` | 0 行 / 0 字节 |
| `memory/RECENT_CONTEXT.md` | 0 行 / 0 字节 |
| `memory/RECENT_CONTEXT.json` | 1 行 / 3 字节 |
| PostgreSQL sessions | 599 行 |
| PostgreSQL messages | 10,618 行 |
| PostgreSQL memory_archive | 81 行 |
| Qdrant `taleclaw_history` | 71 points |
| Qdrant source types | 68 `session_turn`，3 `memory_file` |
| Qdrant scope prefixes | 71 个均为 `user` |

这些数字是环境快照，不是迁移输入清单；执行迁移前必须重新统计。

可重复的无正文文件统计：

```bash
wc -l -c memory/SELF.md memory/MEMORY.md memory/NOW.md memory/PENDING.md \
  memory/PENDING.json memory/HISTORY.md memory/RECENT_CONTEXT.md \
  memory/RECENT_CONTEXT.json
```

数据库和 Qdrant 统计脚本必须只查询 relation count、point count、source type 和 scope prefix，禁止输出 `content`、`text`、`user_text` 或 message payload。

## 13. 重复副本与废弃候选

| 当前载体/接口 | 目标分类 | 迁移前处置 |
|---|---|---|
| Session Store | Session History 真源 | 保留 |
| `memory_archive` | legacy episodic archive | 保留，只读 |
| `MEMORY.md` | semantic legacy source | dry-run 导入后改为只读导出 |
| `PENDING.json` | structured candidate legacy source | 校验格式后导入 candidate |
| `PENDING.md` | 漂移副本/人工复核 | 不自动 active |
| `HISTORY.md` | 重复历史归档 | 不导入 semantic，停止普通写入 |
| `RECENT_CONTEXT.*` | 重复近期窗口 | 不导入 semantic，停止普通写入 |
| SELF/NOW | agent/task 状态兼容 | 默认 review/skip |
| Qdrant `memory_file` | 可废弃派生 point | 新索引验证后清理，不能先删 |
| `MemoryStore.append/recall` | legacy adapter | 生产长期写入/读取迁走后 deprecated |

## 14. 渐进迁移风险与保护措施

- 不能把 HISTORY、RECENT_CONTEXT 或未确认 Pending 批量写成 active memory。
- 不能双向同步 PostgreSQL 与 Markdown，否则冲突时无法判断真源。
- 不能在数据库事务内调用 Qdrant；必须用 outbox 最终一致。
- 不能先删除旧文件或 collection；先建立基线、dry-run、报告和可重建索引。
- 不能仅在召回后裁剪跨 Session 数据；过滤条件必须在向量查询阶段携带可信 session/task/workspace/project。
- 不能让模型提供 owner ID；作用域必须来自服务端 Session 与 Workspace Resolver。
- 当前 PENDING.json 格式异常，importer 必须把它报告为失败或人工复核，不能猜测修复。

## 15. 审计结论

高耗时与混乱不是单个文件过大造成的单点问题，而是同一信息被重复摘要、写文件、归档、嵌入和召回，加上用户级历史向量 scope 导致跨 Session 原始消息进入新上下文。重构应先建立 PostgreSQL semantic truth 与 outbox，再迁移显式记忆；随后统一候选/Coding，最后收紧 episodic boundary 并退役 Markdown 主链路。
