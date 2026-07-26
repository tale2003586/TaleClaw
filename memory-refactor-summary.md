# TaleClaw 长期记忆系统渐进式重构总结

日期：2026-07-26

分支：`refactor/long-term-memory-system`

基线提交：`9643cb747693fab17d531fa2cdfd10f36e715ccc`

## 1. 原问题

旧实现把长期事实、候选推断、原始会话历史、近期上下文和 Coding 局部状态混放在 Markdown/JSON 与同一类向量召回链路中。普通聊天的 episodic 查询只有 user scope，导致新 Session 可能召回旧 Session 的原始消息；整份 `MEMORY/HISTORY/RECENT_CONTEXT` 文件还会被重复写入和索引。长期事实缺少统一 owner、状态、版本、证据、冲突和撤销模型，PostgreSQL 与 Qdrant 的事实来源关系也不明确。

## 2. 已完成的改动

- 新增独立领域模型、命令、Repository 协议和 PostgreSQL 实现。
- 将 PostgreSQL 定义为 semantic memory 唯一事实来源，将 Qdrant 定义为可重建派生索引。
- 实现 remember、propose、confirm、reject、update、revoke、forget、冲突版本链与证据合并。
- 用 transactional outbox 隔离数据库提交和 Qdrant 同步，并支持幂等、失败重试和陈旧事件抑制。
- 将 Semantic Memory、Episodic History、Working Memory 拆成不同结果类型、边界和 Context 分区。
- 普通 episodic 检索在向量查询阶段强制 `user_id + current session_id`；Coding 使用受信任 Task/Workspace/Project 边界。
- 系统推断与 Coding Conclusion 统一先形成 candidate/proposal，不再直接写成用户级 active 事实。
- 默认停止普通流程写 `HISTORY.md`、`RECENT_CONTEXT.*` 和索引整份 legacy 文件；保留受 feature flag 控制的兼容适配器。
- 新增 legacy importer、PostgreSQL 单向 Markdown exporter、Qdrant rebuild CLI 和迁移 Runbook。
- 新增 memory Trace 事件缓冲、运行时汇聚和写入、候选、晋升、拒绝、冲突、无效/陈旧/越界召回、索引失败及 Context token 指标。

## 3. 新架构

详细设计见 `docs/architecture/MEMORY_ARCHITECTURE.md`。核心关系如下：

```text
受信任 Session/Application 上下文
          ↓
命令与候选治理服务
          ↓
PostgreSQL memory_items / evidence / outbox  ← 唯一事实来源
          ↓ 异步、幂等、可重试
Qdrant semantic collection                  ← 可重建派生索引
          ↓ 命中后 PostgreSQL 回源验证
<semantic_memory> Context

当前 Session/Task 事件 → 带边界 Qdrant 查询 → <episodic_history>
Working Memory        → 现有运行时状态       → <working_memory>
```

## 4. 关键数据流

显式记忆可直接形成 active；推断和 Coding 结论先形成 candidate。所有写入先验证 trusted owner，再做规范化、精确去重、语义重复/冲突判定，在同一数据库事务内保存事实、证据、状态和 outbox。检索先从 Qdrant 取候选 ID，再回 PostgreSQL 检查 owner、active、有效期和当前版本；索引不可用时只在相同 trusted owners 内做 PostgreSQL 降级，不扩大作用域。

原始 turn 只作为 event 粒度的 episodic 数据。普通会话缺少当前 Session 边界时直接返回空，不再退化为 user-only 查询。跨 Session 可共享的是经过治理的 semantic fact，而不是另一 Session 的原始消息。

## 5. 迁移与回滚

迁移步骤、文件分类、checksum、dry-run、apply、索引重建、feature flag 灰度和回滚见 `docs/migrations/long-term-memory-migration.md`。

- importer 默认 dry-run，幂等且不删除或改写源文件；`MEMORY.md` 有效条目可导入 active，Pending 为 candidate/review，HISTORY/RECENT 不导入 semantic active。
- exporter 只从 PostgreSQL active 数据单向生成带只读标识的 Markdown。
- rebuild 默认 dry-run，应写入新 collection 后再切换。
- 回滚只关闭 Context/read/write/service flags，保留 PostgreSQL 事实和 outbox；不 DROP 表、不删除 legacy 文件或 collection。

本次没有对真实 legacy 数据执行 `--apply`，没有删除文件、表或 Qdrant collection，也没有切换生产 feature flag。

## 6. 兼容性

所有 semantic 开关默认关闭，可按 service → write → read → Context 顺序启用。普通 legacy history 文件写入默认关闭；Coding task-local 状态继续保留。`MemoryStore` 被明确标记为 legacy adapter，现有 Session 恢复、工具可见性/授权、Security RAG、Working Memory 和 Coding 生命周期均由完整测试覆盖。

Context 公共分区名从 `retrieved_history` 有意改为 `episodic_history`，对应基线快照已更新。这是为了消除“历史召回等于长期事实”的语义混淆。

## 7. 验证结果

执行结果：

```text
python -m pytest -q
556 passed, 2 warnings in 37.16s

python -m pytest -q tests/test_runtime_phase0_contract_baseline.py
3 passed in 0.11s

python -m compileall -q memory runtime applications tools scripts
通过

git diff --check
通过
```

2 条 warning 均来自 `qdrant-client` 所依赖 protobuf 类型针对 Python 3.14 的弃用提示，不是本次行为失败。定向 memory 套件、真实 PostgreSQL 隔离 schema 测试、fake Qdrant 故障/重建测试和全仓回归均已通过。

## 8. 验收清单结果

状态定义：自动通过＝本地自动测试/静态命令已有证据；隔离模拟通过＝故障或外部服务由测试替身验证；部分验证＝核心行为通过但缺少清单要求的附加人工/工具验证；未执行＝需要真实服务、真实数据副本或部署切换，未擅自执行。

汇总：自动通过 49 项，隔离模拟通过 5 项，部分验证 2 项，未执行 3 项，共 59 项。

| 项目 | 状态 | 证据或说明 |
|---|---|---|
| C01 | 自动通过 | 分支、基线和提交链可追溯；最终变更均属于本重构 |
| C02 | 自动通过 | audit、architecture、migration、summary 文档齐全，无占位符 |
| C03 | 未执行 | 未启动或切换真实 PostgreSQL/Qdrant 验收环境 |
| C04 | 部分验证 | 已只读审计 legacy 文件；未替用户制作真实数据备份 |
| C05 | 自动通过 | command、context、跨 Session semantic 测试 |
| C06 | 自动通过 | update/supersede 版本链与 Context 测试 |
| C07 | 自动通过 | revoke/forget、outbox、retrieval 负向测试 |
| C08 | 自动通过 | promotion 单次推断保持 candidate |
| C09 | 自动通过 | scope 矩阵与跨 owner 拒绝测试 |
| C10 | 自动通过 | `test_coding_memory_proposals.py` |
| C11 | 自动通过 | episodic 跨 Session 隔离与 semantic 跨 Session 测试 |
| C12 | 自动通过 | 当前 Session episodic 与 `past_events` 分区测试 |
| C13 | 自动通过 | semantic 状态、有效期、scope 回源过滤测试 |
| C14 | 自动通过 | stale/missing/rebuild 测试 |
| C15 | 自动通过 | importer dry-run、幂等、报告测试 |
| C16 | 自动通过 | 七类 legacy 来源分类测试 |
| C17 | 自动通过 | Trace memory 分类和 summary 指标测试 |
| C18 | 隔离模拟通过 | fake index 故障时数据库提交、重试和降级测试 |
| C19 | 自动通过 | 全仓 556 项回归通过 |
| C20 | 自动通过 | domain/repository round-trip 测试 |
| C21 | 自动通过 | 领域验证、非法转换回滚测试 |
| C22 | 隔离模拟通过 | payload 最小化与 rebuild 测试；未删除真实 collection |
| C23 | 自动通过 | domain/protocol 单测与导入边界实现 |
| C24 | 自动通过 | 命令结果及 Trace 事件测试 |
| C25 | 自动通过 | exact dedup 与 evidence 幂等测试 |
| C26 | 自动通过 | Context ID/内容去重测试 |
| C27 | 自动通过 | `test_memory_conflicts.py` |
| C28 | 自动通过 | revoke/expire/forget 检索与索引测试 |
| C29 | 自动通过 | expected-version 乐观并发控制与事务测试 |
| C30 | 自动通过 | 三类 Context 分区测试 |
| C31 | 自动通过 | PostgreSQL 回源、状态/scope/排序测试 |
| C32 | 自动通过 | 可记录 fake index 验证查询前过滤条件 |
| C33 | 自动通过 | 缺边界返回空及 Coding trusted boundary 测试 |
| C34 | 自动通过 | 独立预算、去重和 Context report 测试 |
| C35 | 自动通过 | Qdrant 异常的 scope-limited fallback 测试 |
| C36 | 自动通过 | outbox upsert/delete 生命周期测试 |
| C37 | 自动通过 | 索引失败不回滚事实测试 |
| C38 | 自动通过 | claim/complete/retry 与幂等同步测试 |
| C39 | 自动通过 | event 粒度索引及 whole-file 停写测试 |
| C40 | 自动通过 | legacy 文件校验与只读 exporter 测试 |
| C41 | 自动通过 | importer 坏数据、重复运行、源文件不变测试 |
| C42 | 自动通过 | 七类来源唯一处置测试 |
| C43 | 未执行 | 未在真实部署演练迁移、collection 切换与 flag 回滚 |
| C44 | 自动通过 | memory Trace schema、限长 preview 测试 |
| C45 | 自动通过 | 全指标及零分母 summary 测试 |
| C46 | 部分验证 | 类型注解、compileall、真实调用方通过；仓库未配置 mypy/ruff |
| C47 | 自动通过 | 定向、全量、compileall 全部通过 |
| C48 | 自动通过 | diff check、状态、大文件和敏感模式检查通过 |
| E01 | 隔离模拟通过 | 跨 Session semantic 写入/读取 fixture |
| E02 | 自动通过 | 双 Session episodic 隔离测试 |
| E03 | 自动通过 | update → supersede → revoke 组合测试 |
| E04 | 自动通过 | 独立 Evidence 晋升测试 |
| E05 | 自动通过 | Coding trusted scope/proposal 隔离测试 |
| E06 | 隔离模拟通过 | Qdrant 故障、outbox retry、恢复同步测试替身 |
| E07 | 隔离模拟通过 | legacy fixture dry-run/apply/idempotency/rebuild；未用真实副本 apply |
| E08 | 未执行 | 未在真实部署执行 flag 关闭/重开演练 |
| S01 | 自动通过 | 本表逐项覆盖 C01–C48、E01–E08 和 S01–S03 |
| S02 | 自动通过 | 所有非完全通过项均列明原因、影响与下一步 |
| S03 | 自动通过 | Spec、Plan、Task、Checklist 与测试文件形成追踪链 |

## 9. 未执行项、影响与方案

- C03、C43、E08：缺少经授权的真实验收环境和部署切换窗口。影响是无法证明真实网络、凭据、服务版本及运维切换行为。按 Runbook 在隔离 staging 演练；失败时关闭四个 semantic flags，保留事实和 outbox。
- C04：未制作真实 legacy 数据备份。正式 apply 前必须生成 checksum 和只读备份；若校验不一致立即停止，不执行导入。
- C46：仓库没有 mypy/ruff 配置，无法宣称这些工具通过。当前以类型注解、compileall、测试与公共入口真实调用替代；后续可单独引入静态检查基线。
- E01、E06、E07 以及 C18、C22 使用隔离数据库或测试替身，不等价于真实 Qdrant/生产数据验证。Runbook 已给出真实服务验证和回滚步骤。

## 10. 风险与后续建议

1. 先在 staging 创建新 semantic collection，运行 importer dry-run 并人工审核 review/conflict/failed，不要直接对真实数据 apply。
2. 为 PostgreSQL outbox backlog、retry age、scope-drop 和 stale-hit 指标设置告警，再逐步开启 write、read、Context。
3. 观察至少一个发布周期后再讨论 legacy 文件/旧 collection 清理；清理必须是独立审批和可恢复操作。
4. 升级或约束 qdrant/protobuf 依赖前，在 Python 3.14 环境复测当前两条弃用警告。
5. 后续新增 Application 必须通过 proposal 接口和 trusted owner，不得直接写用户级 active memory。
