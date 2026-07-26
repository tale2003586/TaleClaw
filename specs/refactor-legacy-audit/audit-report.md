# 生产代码重构遗留审计报告

## 结论

本次审计覆盖全部生产 Python 目录、八个 PostgreSQL 相关模块以及当前连接实例。共确认并修复 7 类问题：

1. 会话身份迁移只新增 `active_agent`，未清理旧非空字段，导致新写入失败。
2. 飞书 outbox 新建结构支持文档消息，但旧表没有对应增量升级。
3. Trace 索引新增的 nullable/default 字段只存在于新建 SQL，旧表无法获得。
4. 测试目录同时使用 package import 和裸模块 import，被环境同名 `tests` 包遮蔽。
5. token 快照声明允许估算器版本小幅误差，断言却要求绝对相等。
6. 架构扫描把 macOS `._*.py` 元数据当作 UTF-8 Python 源码。
7. 普通 trace 测试在设置共享 `DATABASE_URL` 时会把索引写入当前实例。

生产兼容入口未发现可以无风险机械删除的项。所有保留项均有当前调用方、协议测试或数据兼容用途。

## 审计范围与方法

- 生产代码：`agents/`、`applications/`、`runtime/`、`tools/`、`memory/`、`gateway/`、`web/`、`models/`、`plugins/`、`skill_runtime/`。
- PostgreSQL：会话、Web 鉴权、记忆归档、Telegram、飞书、trace 索引、工具结果，以及公共 DB 层。
- 排除：历史报告、实验脚本、security RAG legacy 基线和 benchmark fixtures。
- 证据：静态调用方扫描、建表/读写字段核对、临时 PostgreSQL schema、当前实例 catalog 查询、专项测试和全量测试。

## 已修复

| 编号 | 候选项 | 模块 | 风险 | 修复 | 验证 |
|---|---|---|---|---|---|
| F-01 | 会话旧身份字段残留 | `runtime/sessions/session_store.py` | 新代码不再写旧非空列，触发 `NotNullViolation` | 在显式事务中承接旧值、验证目标值、收紧约束并删除旧列 | 旧表、半迁移表、空值回填、消息保全、二次初始化、失败回滚测试 |
| F-02 | 飞书文档 outbox 缺少旧表升级 | `gateway/feishu/store.py` | 旧部署调用文档入队时缺列失败 | 增加与 Telegram 对称的 `message_type`、`document_path`、`caption` 幂等迁移 | 旧文本消息保留，新文档消息写入，重复初始化 |
| F-03 | Trace 安全新增字段缺少迁移 | `runtime/trace/index_store.py` | 旧表 upsert 新字段时缺列失败 | 只补 nullable 或带数据库默认值的字段，不猜测核心必填值 | 最小旧表、已有行保留、当前 upsert、重复初始化、完整字段契约 |
| F-04 | PostgreSQL 字段盘点 SQL 重复 | `runtime/db.py`、gateway/session stores | 各 store 的 schema 范围判断可能漂移 | 抽取只读 `table_columns`，统一使用 `current_schema()` | 临时 search path 字段测试 |
| F-05 | 测试包导入边界冲突 | `tests/__init__.py`、12 个 PostgreSQL 测试 | 环境同名包导致 10 个模块收集失败 | 显式声明项目测试包，统一 `tests.postgres_utils` | 全量测试成功收集并执行 |
| F-06 | 快照/源码扫描与既定意图不一致 | 两个 runtime baseline 测试 | 估算器微差和 AppleDouble 文件造成伪失败 | token 允许窄幅 ±2；源码扫描忽略 `._` 元数据 | 对应测试和全量测试通过 |
| F-07 | 普通测试写入共享 trace index | `tests/conftest.py` | 全量测试向当前实例累计测试 run/step | 普通测试默认关闭共享 trace index；显式 store 测试继续使用临时 schema | 清理前精确识别 45/117 条测试记录；清理后再次全量测试，当前实例行数不变 |

## PostgreSQL Store 契约核对

| Store | 所有表 | 新建结构 | 升级路径 | 读写闭合 | 约束/索引 | 结论 |
|---|---|---:|---:|---:|---:|---|
| SessionStore | `sessions`, `messages` | 通过 | 已修复 | 通过 | PK/FK/NOT NULL 通过 | fixed |
| WebAuthStore | `web_users`, `web_auth_sessions` | 通过 | 无可证明旧字段演进 | 通过 | PK/FK/expiry index 通过 | retained |
| MemoryArchiveStore | `memory_archive` | 通过 | 无可证明旧字段演进 | 通过 | PK/UNIQUE/index 通过 | retained |
| TelegramGatewayStore | 3 张 Telegram 表 | 通过 | 文档字段已有幂等升级 | 通过 | PK/outbox index 通过 | retained + strengthened tests |
| FeishuGatewayStore | 3 张飞书表 | 通过 | 文档字段升级已补齐 | 通过 | PK/outbox index 通过 | fixed |
| TraceIndexStore | `trace_runs`, `trace_steps` | 通过 | 安全新增字段升级已补齐 | 通过 | PK/FK/3 indexes 通过 | fixed |
| PostgresToolResultStore | `tool_results` | 通过 | 无可证明旧字段演进 | put/get 通过 | PK/session index 通过 | retained |
| DB 基础层 | 无业务表 | 不适用 | 不持有业务 DDL | 参数与 dict row 通过 | current schema 识别通过 | fixed |

对于 Web 鉴权、记忆归档和工具结果，未发现能够证明来源与安全回填规则的旧结构。按照规格要求，没有为未知必填字段制造默认业务值。

## 明确保留的兼容项

| 候选项 | 分类 | 保留证据 |
|---|---|---|
| `AgentSpec.profile` / `from_profile` | retained | 当前 routing、context、runner 生产调用链仍使用；Phase 1 行为测试覆盖 |
| `CHILD_TOOLS` / `PARENT_TOOLS` | retained | 文件明确声明为旧导入兼容别名；内部新代码已使用 TEAMMATE/LEAD 名称，保留不会影响当前语义 |
| 子代理旧 explore payload | retained | `test_subagent_output_protocol` 明确验证旧 payload 到 v1 schema 的协议适配 |
| `ScopedMemoryStore.legacy_store` | retained | bootstrap 仍注入旧 memory 根目录，用于存量数据路径兼容 |
| security RAG `legacy_search` | retained | tiered route 未产生 hits 时的实际搜索阶段，不是废弃身份字段 |
| result compression legacy descriptor/placeholder | retained | 用于已存在的全局结果与无引用结果压缩表现，属于数据兼容 |
| trace/memory/tool/retrieval 的 `mode` | retained | 分别表示运行记录、归档上下文、权限视图和检索策略，语义不同于 session agent identity |
| 模型、网络、摘要和 embedding fallback | retained | 正常容错或离线开发策略，与重构字段迁移无关 |

## 范围排除

- 历史文档中出现的 `current_mode`、旧 Phase 报告和路线图。
- security RAG 的 legacy collection、对照实验脚本和历史指标。
- benchmark fixtures 中为评测准备的旧格式。
- macOS `._*` 元数据文件的批量删除。测试已避免误解析；是否清理归档元数据属于独立仓库卫生任务。

## 待人工决策

没有阻断本次修复的数据库项。

`AgentSpec.profile` 仍是活跃生产桥接层，但注释把它描述为仅供外部 fixture 使用，与事实不完全一致。完全移除它需要单独的路由/context 接口改版和公开兼容策略，不适合作为本次缺陷清理中的机械删除，因此保留并记录为后续架构议题。

## 当前 PostgreSQL 实例

### 迁移前摘要

- 13 张已启用业务表，111 个字段，92 个约束记录，21 个索引。
- 行数：

| 表 | 行数 |
|---|---:|
| sessions | 598 |
| messages | 10,606 |
| web_users | 1 |
| web_auth_sessions | 1 |
| memory_archive | 77 |
| telegram_state / conversations / outbox | 0 / 0 / 0 |
| feishu_events / conversations / outbox | 0 / 0 / 0 |
| trace_runs | 1,755 |
| trace_steps | 20,753 |

- `active_agent`、Web 用户必填值、archive 身份、两个 outbox 核心字段和 trace 核心字段的无效值计数均为 0。
- `sessions.current_mode` 已不存在，`sessions.active_agent` 为 `NOT NULL`。
- `tool_results` 未创建：当前运行配置使用文件 backend；其 PostgreSQL backend 已在临时 schema 中验证，未对当前实例创建未启用的表。

### 初始化与隔离读写

以下实际 store 使用当前实例初始化成功：

- SessionStore
- WebAuthStore
- MemoryArchiveStore
- TelegramGatewayStore
- FeishuGatewayStore
- TraceIndexStore

随后分别执行可清理的 session、auth session、archive、Telegram outbox、飞书事件和 trace run/step 读写验证。`finally` 清理完成后，13 张表行数与迁移前逐表一致。

全量测试曾暴露普通 `TraceStore` 测试会复用共享 trace index。三个明确的测试时间批次共写入 45 条 run 和 117 条 step，经精确计数后事务删除，并增加自动测试 fixture。修复后再次运行全部 488 项测试，`trace_runs` / `trace_steps` 仍保持 1,755 / 20,753。

### 迁移后摘要

- 表、约束、索引及核心字段有效性检查通过。
- 所有迁移重复初始化无异常。
- 业务行数无变化，隔离验证记录无残留。

## 测试证据

| 验证 | 结果 |
|---|---|
| 修改模块 `compileall` | 通过 |
| 第一批 DB/网关专项 | 44 passed |
| 其余 store 契约专项 | 22 passed |
| 汇总 PostgreSQL store 专项 | 76 passed |
| 生产兼容入口专项 | 53 passed |
| Session 事务/迁移专项（最终） | 6 passed |
| 全量测试（最终） | 488 passed, 2 third-party deprecation warnings |
| 最终真实 Python 源码编译 | 通过（排除非源码 `._*` 元数据） |
| 最终数据库复核 | 0 个临时 schema；关键表行数与基线一致 |

全量命令使用已配置的 `DATABASE_URL`：

```bash
python -m pytest -q
```

两条 warning 来自 protobuf 扩展对 Python 3.14 的未来弃用提示，与本次变更无关。

## 验收映射

- AC1 / AC4 / AC6 / AC8：候选项已按修复、保留、排除、待决分类并附调用方/测试证据。
- AC2：八个 PostgreSQL 相关模块完成建表、升级、读写、约束和索引核对。
- AC3 / AC5：旧库、半迁移库、新库、重复初始化、失败回滚及数据保全测试通过。
- AC7：当前实例前后快照、全部启用 store 初始化和隔离读写通过。
- AC9：无业务数据丢失、半迁移、无依据兼容删除、凭证泄露或历史基线修改。
