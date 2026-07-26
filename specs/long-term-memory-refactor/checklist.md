# TaleClaw 长期记忆系统渐进式重构 Checklist

> 本清单在实现完成后逐项执行。每项必须记录实际命令、关键输出或可观察结果；仅阅读代码不能判定通过。

## 验收前置条件

- [ ] C01 当前分支和提交可追溯，工作区不含意外修改。（验证：运行 `git status --short --branch` 和 `git log -5 --oneline`，记录分支、基线提交与待验收提交）
- [ ] C02 审计、架构、迁移和总结文档均存在且不含占位符。（验证：运行 `test -f docs/refactors/memory-system-audit.md && test -f docs/architecture/MEMORY_ARCHITECTURE.md && test -f docs/migrations/long-term-memory-migration.md && test -f memory-refactor-summary.md`，再运行 `rg -n 'TODO|TBD|PLACEHOLDER|待补充'`，期望没有未解释的占位符）
- [ ] C03 验收环境明确记录 PostgreSQL、Qdrant、feature flag 和测试数据隔离方式。（验证：按迁移文档启动测试依赖，记录服务健康检查和非生产数据标识）
- [ ] C04 旧数据在迁移前有只读统计或备份，验收不会删除源文件。（验证：运行迁移文档中的迁移前检查，比较源文件清单与校验值）

## Spec 验收标准

- [ ] C05 显式“记住我的偏好”会产生用户级 active 记忆，PostgreSQL 中存在事实与证据，同一用户的新 Session 可检索，且流程不依赖 Markdown。（覆盖：AC1；验证：执行显式记忆端到端测试，并暂时禁用 legacy Markdown 读取后重复检索）
- [ ] C06 更新已有偏好会生成新版本并把旧版本标记为 superseded，Context 只包含新版本一次。（覆盖：AC2；验证：运行 `python -m pytest -q tests/test_memory_command_service.py tests/test_memory_context_sections.py -k 'update or supersed'`）
- [ ] C07 遗忘已有记忆后，PostgreSQL 状态为 revoked，索引删除进入成功或待重试状态，检索和 Context 均不再返回它。（覆盖：AC3；验证：运行遗忘集成测试并查询事实表、outbox、检索结果和 Context report）
- [ ] C08 单次系统推断只创建 candidate，不会直接成为 active 或跨 Session 注入。（覆盖：AC4；验证：运行 `python -m pytest -q tests/test_memory_promotion_service.py -k 'single or candidate'`）
- [ ] C09 Project/Workspace A 的事实不能被 Project/Workspace B 检索或注入，用户级共享记忆仍按规则可见。（覆盖：AC5；验证：运行跨项目矩阵测试，观察 A 命中、B 零命中）
- [ ] C10 Coding Conclusion 通过统一 proposal 进入治理链路，包含 workspace、repository revision、task 和证据；未验证结论及 task-local 状态不能成为用户级 active 记忆。（覆盖：AC6；验证：运行 `python -m pytest -q tests/test_coding_memory_proposals.py`）
- [ ] C11 新普通 Session 不会命中同一用户其他 Session 的原始 turn，但可以命中该用户的 active semantic memory。（覆盖：AC7；验证：运行 `python -m pytest -q tests/test_episodic_history_scope.py tests/test_semantic_memory_retrieval.py -k 'cross_session or new_session'`）
- [ ] C12 当前 Session 的旧 turn 可以作为 episodic history 命中，并在输出中明确标记为过去事件而非当前事实。（覆盖：AC8；验证：运行当前 Session 历史端到端测试并检查 Context 分区标签）
- [ ] C13 superseded、revoked、expired、rejected 以及作用域不匹配的记忆都不会进入 Context。（覆盖：AC9；验证：运行状态与作用域参数化测试，期望所有无效样本零注入）
- [ ] C14 Qdrant 缺失、返回旧版本或返回无效状态向量时，PostgreSQL 回源会丢弃陈旧命中；重建后索引与 active 当前版本一致。（覆盖：AC10；验证：运行 `python -m pytest -q tests/test_semantic_memory_retrieval.py tests/test_memory_index_rebuild.py -k 'stale or missing or rebuild'`）
- [ ] C15 importer dry-run 零写入；相同输入重复执行不会重复导入；报告分别统计 imported、skipped、conflicted 和 failed。（覆盖：AC11；验证：运行 `python -m pytest -q tests/test_legacy_memory_importer.py -k 'dry_run or idempotent or report'`）
- [ ] C16 legacy `MEMORY.md` 的有效条目迁移后可检索；`HISTORY.md`、`RECENT_CONTEXT.*` 和未确认 Pending 不会导入为 active。（覆盖：AC12；验证：用隔离 fixture 执行迁移并比较导入报告、数据库状态和检索结果）
- [ ] C17 Trace 能区分 semantic memory、episodic history、candidate promotion 和 index sync，并能计算跨作用域泄漏与陈旧命中指标。（覆盖：AC13；验证：运行 `python -m pytest -q tests/test_run_trace.py -k memory` 并检查汇总字段）
- [ ] C18 PostgreSQL 可用而 Qdrant 不可用时，显式写入仍成功提交，outbox 保留可重试事件，聊天请求不会因索引故障失败。（覆盖：AC14；验证：停止测试 Qdrant 后执行写入与一次聊天，再恢复 Qdrant 并 drain outbox）
- [ ] C19 Session 恢复、Working Memory、Coding、Context Builder、工具安全和 Security RAG 的既有行为没有回归。（覆盖：AC15、N10；验证：运行仓库完整测试及相关定向测试，记录结果）

## 领域与事实来源

- [ ] C20 每条长期记忆都可观察到稳定 ID、可信 owner、受控 kind/status、置信度、显著性、来源、证据、有效期、版本、替代关系和审计时间。（覆盖：F1–F5；验证：创建各类型样本并通过公开查询或测试辅助接口检查持久化结果）
- [ ] C21 非法 owner、kind、status、时间范围、版本或状态转换会被明确拒绝，且不会产生部分数据库写入。（覆盖：F2–F4、N3；验证：运行领域和 Repository 的参数化失败测试，随后确认表与 outbox 无残留）
- [ ] C22 Qdrant point 只包含检索所需内容和元数据；删除 Qdrant collection 后仍可从 PostgreSQL 恢复全部 active semantic memory。（覆盖：F5–F6、F25；验证：在隔离环境重建 collection 并比较 active ID/version 集合）
- [ ] C23 领域与 Repository 公共接口可在不导入 PostgreSQL、Qdrant、Markdown、Application 或模型 SDK 的环境中加载。（覆盖：N2、N6；验证：运行领域单元测试和依赖扫描，期望无基础设施类型泄漏）

## 写入、冲突与生命周期

- [ ] C24 显式记忆、候选、Coding Conclusion、确认、拒绝、更新、撤销和遗忘均产生统一格式的结果与 Trace。（覆盖：F7–F11、F32–F33；验证：对每种命令运行集成测试并比较事件必填字段）
- [ ] C25 相同 owner、kind 和规范化内容的精确重复不会创建第二条 active 记忆，只合并证据或更新确认时间。（覆盖：F12；验证：连续提交等价文本，比较 active 数量、版本和 evidence 数量）
- [ ] C26 语义重复不会在同一次 Context 中重复注入。（覆盖：F13、F22；验证：建立两个相似候选或 legacy 重复样本，观察 Context 只保留一次）
- [ ] C27 同作用域冲突事实形成可审计的新旧版本链，不同作用域的相似事实不会相互 supersede。（覆盖：F14；验证：运行 `python -m pytest -q tests/test_memory_conflicts.py`）
- [ ] C28 撤销、过期和遗忘都会停止后续索引与 Context 注入，同时保留最小审计记录且不记录不必要的全文。（覆盖：F15、F33；验证：执行三类状态变化后检查数据库、outbox、Trace 和检索）
- [ ] C29 并发更新同一 active 版本时最多一个提交成功，失败方收到可分类的版本冲突。（覆盖：N3；验证：运行 Repository 并发测试并确认只有一条当前 active 版本）

## 检索与 Context 边界

- [ ] C30 Semantic Memory 与 Episodic History 使用不同结果类型，并在 Context 中与 Working Memory 分区呈现。（覆盖：F16、F20；验证：运行 `python -m pytest -q tests/test_memory_context_sections.py` 并检查三个分区）
- [ ] C31 semantic retrieval 在进入 Context 前回源过滤 owner、active 状态、有效时间和当前版本，并按相关性、置信度、显著性和新鲜度产生稳定排序。（覆盖：F17、F21；验证：运行排序及无效状态参数化测试）
- [ ] C32 episodic 向量查询在检索请求阶段就携带 session/task/workspace/project 边界，而不是召回后裁剪。（覆盖：F18–F19；验证：使用可记录 filter 的 fake index 运行边界测试并检查实际查询过滤条件）
- [ ] C33 普通聊天不存在只有 user scope 的 episodic 查询；Coding 查询必须具有受信任的 task 或 workspace/project 边界。（覆盖：F18–F19、N5；验证：运行缺少边界的失败测试和正常 Coding 边界测试）
- [ ] C34 Semantic、Episodic 和 Working Memory 分别受预算控制；同一 Memory ID 一次最多注入一次，截断结果在 Context report 中可见。（覆盖：F22；验证：用超预算样本运行 Context Builder 测试并核对 token 与 drop report）
- [ ] C35 Qdrant 不可用时的检索降级不会返回无效状态、旧版本或越界数据。（覆盖：N7；验证：禁用 Qdrant，运行状态和作用域负向检索矩阵）

## 索引一致性与恢复

- [ ] C36 创建、更新、替代、撤销和过期分别产生幂等、可重试的索引事件。（覆盖：F23；验证：运行 `python -m pytest -q tests/test_memory_index_sync.py -k 'create or update or supersede or revoke or expire'`）
- [ ] C37 数据库事务提交期间不执行远程 Qdrant 调用；索引异常不会回滚已提交事实。（覆盖：F24、N4；验证：让 fake index 在同步阶段抛错，确认事实已提交且 outbox 标记重试）
- [ ] C38 重复 drain outbox 不会制造重复 point；失败达到重试时间后可恢复，错误和 attempt count 可观察。（覆盖：F23–F25；验证：运行同步器幂等与退避测试）
- [ ] C39 Session Event 以单个 turn/event 为索引粒度，普通流程不再索引整份 SELF/MEMORY/NOW/PENDING/HISTORY 文件。（覆盖：F26；验证：运行 lifecycle 测试并检查写入 point 的 source type、数量与 payload）

## Markdown 与迁移

- [ ] C40 新的长期记忆写入不会修改 legacy Markdown；Markdown 导出仅由 PostgreSQL active 数据单向生成。（覆盖：F27、F31；验证：记录文件校验值，执行写入后应不变；执行 exporter 后内容与数据库一致）
- [ ] C41 legacy importer 支持 dry-run、幂等键、逐项失败恢复和迁移报告，任何模式都不会自动删除源文件。（覆盖：F28；验证：注入一条坏数据并重复运行 importer，比较报告和源文件校验值）
- [ ] C42 `MEMORY.md`、`PENDING.json`、`HISTORY.md`、`RECENT_CONTEXT.*`、`SELF.md`、`NOW.md` 和 `PENDING.md` 都按迁移文档中的明确规则得到 import、skip、review 或 export 结果。（覆盖：F29–F30；验证：运行包含七类来源的 fixture，报告中每条输入都有唯一处置）
- [ ] C43 migration、index rebuild 和 feature flag 回滚步骤可重复执行，回滚不会删除已迁移事实或源文件。（覆盖：N1、N9、N11；验证：在隔离环境完成“迁移→重建→关闭新读取→重新开启”演练并比较数据计数）

## 可观测性与质量

- [ ] C44 所有 memory Trace 事件包含适用的 ID、scope、kind、version、source、reason、confidence、score、previous ID 和 index status，敏感内容仅保留限长 preview 或摘要。（覆盖：F32–F33；验证：运行事件 schema 测试并检查最大字段长度）
- [ ] C45 指标能够计算写入数、候选数、晋升率、拒绝率、替代率、撤销率、重复率、冲突率、命中率、无效检索率、陈旧命中率、跨作用域泄漏率、索引失败率和 Context token 占比，零分母时不报错。（覆盖：F34；验证：运行有事件与空事件两组 summary 测试）
- [ ] C46 新公共接口具有类型信息、明确错误分类和结构化日志，且所有公共入口至少有一个真实调用方或集成测试。（覆盖：N8；验证：运行仓库现有类型/静态检查与 unused 接口检查；若无统一工具，则在总结报告记录等价检查命令）
- [ ] C47 定向测试、完整测试、编译检查和仓库已有静态检查全部通过。（验证：运行 task.md T51 列出的全部命令，并在总结报告记录通过数、跳过数和耗时）
- [ ] C48 `git diff --check` 无错误，最终变更不包含密钥、生产数据、数据库 dump 或意外生成物。（验证：运行 `git diff --check`、`git status --short` 和仓库现有 secret 检查；人工核对新增大文件）

## 端到端场景

- [ ] E01 跨 Session 长期偏好：Session A 显式记住偏好 → PostgreSQL 提交 → outbox 同步 → Session B 命中 semantic memory → Context 只注入一次。（验证：执行真实服务或端到端 fixture，记录 memory ID、两个 session ID、索引状态和 Context report）
- [ ] E02 跨 Session 历史隔离：Session A 产生一段独特原始对话 → Session B 查询同样关键词 → B 不命中 A 的 episodic turn；A 自身查询可命中并标为历史。（验证：执行双 Session 场景并对比两次检索结果）
- [ ] E03 更新与遗忘：创建偏好 → 更新为冲突新值 → 旧值被替代 → 执行遗忘 → 新旧值均不再进入 Context，审计链仍可追踪。（验证：记录每一步版本、状态、outbox 和 Context 结果）
- [ ] E04 候选晋升：一次推断只创建 candidate → 独立 Session 提供支持证据或用户确认 → 按策略晋升 active → 后续 Session 可检索。（验证：运行 promotion 端到端测试并记录证据来源和状态变化）
- [ ] E05 Coding 隔离：Task A 生成已验证 conclusion → 形成带 workspace/project/task/revision 的 proposal → 同边界可检索；Task B 或用户全局上下文不可见越界内容。（验证：执行双 Task/Workspace 场景并比较 Context）
- [ ] E06 索引故障恢复：关闭 Qdrant → 写入成功且聊天继续 → 观察 pending/retry → 恢复 Qdrant 并同步 → 新 Session 可命中，且无重复 point。（验证：记录故障前后数据库、outbox 和 collection 计数）
- [ ] E07 Legacy 迁移：对真实数据副本先 dry-run → 审核分类报告 → 正式导入 → 重复导入 → 重建索引 → 有效 MEMORY 可检索且历史/Pending 不会误成 active。（验证：比较两次报告、数据库计数、源校验值与检索结果）
- [ ] E08 渐进回滚：完成一个迁移阶段后关闭对应 feature flag → 普通聊天与 Coding 仍可运行 → 重新启用后已写入事实和待同步事件不丢失。（验证：按迁移文档演练并运行 smoke tests）

## 最终签收

- [ ] S01 `memory-refactor-summary.md` 逐项记录 C01–C48 与 E01–E08 的通过、失败或不适用状态，并附实际证据。（验证：清单总数与报告结果总数一致）
- [ ] S02 所有失败项都有原因、影响、修复或回滚方案；不存在以“预计通过”代替实际执行的项目。（验证：人工复核总结报告中的未通过和跳过部分）
- [ ] S03 Spec 的 AC1–AC15 均至少有一项通过证据，F1–F34 与 N1–N11 均能追溯到本清单或明确的非适用说明。（验证：运行文档中的需求追踪检查或人工核对覆盖矩阵）
