# 生产代码重构遗留审计与修复 Plan

## 架构概览

本次工作采用“静态清单 → 结构契约核对 → 定向迁移修复 → 分层验证 → 实例复核”的闭环，不引入新的常驻迁移框架。迁移仍由各 store 在初始化时负责，避免增加第二套 schema 所有权来源。

### 1. 审计清单层

对生产目录中的建表 SQL、结构变更 SQL、查询/写入字段、兼容注释和旧命名进行扫描。候选项进入一份审计矩阵，并按证据分类。扫描范围覆盖八个数据库相关模块：公共数据库连接、会话、Web 鉴权、记忆归档、Telegram、飞书、trace 索引和工具结果存储。

### 2. Store 结构契约层

逐个 store 建立当前结构契约：表、字段、约束、索引、外键，以及初始化时允许接收的旧结构。核对 `CREATE TABLE IF NOT EXISTS`、迁移语句、SELECT、INSERT/UPSERT 和反序列化字段是否闭合。每个 store 继续拥有自己的初始化和升级逻辑；公共数据库模块只提供连接与安全的元数据查询能力，不持有业务 schema。

### 3. 定向迁移与兼容清理层

只对审计中证实的真实遗留修改对应 store。迁移顺序统一为：识别当前结构、补充目标字段、集合式承接数据、验证目标字段、收紧约束、删除冲突旧结构、提交事务。已有明确依据的协议适配、导入别名和领域回退不进入删除路径，只在审计报告记录保留原因。

### 4. 回归验证层

数据库测试使用独立 PostgreSQL 临时 schema，针对每个修复覆盖全新结构、旧结构升级、已迁移结构重复初始化。非数据库修复使用现有调用方测试或新增最小行为测试。再运行 store 专项测试和全量测试，区分本次回归与环境依赖型失败。

### 5. 当前实例核对与审计报告层

先对当前 PostgreSQL 实例采集不含业务正文的结构、约束和行数摘要；仅在修复已有测试覆盖且数据保全检查通过时执行迁移。迁移后重复采集摘要并做隔离读写验证。最终报告将每个候选项归入“已修复、明确保留、范围排除、待人工决策”，并附扫描、测试和数据库证据。

## 核心数据结构

### AuditFinding

审计报告中的一行逻辑记录，不作为生产运行时类型引入。字段包括：

- `candidate`：候选旧字段、兼容入口或迁移路径。
- `module`：所有权模块或 store。
- `category`：`fixed`、`retained`、`excluded`、`decision_required`。
- `evidence`：代码位置、测试、协议说明或数据库查询证据。
- `risk`：不处理或误删除的可观察影响。
- `action`：实际修复、保留理由或后续决策。
- `verification`：验证命令和结果摘要。

### StoreSchemaContract

用于设计和测试的结构契约，不要求新增生产类。每个 store 的核对记录包含：

- store 所拥有的表集合。
- 每张表的目标字段、空值约束、默认值、主外键和必要索引。
- 可以识别的旧结构签名。
- 旧字段到目标字段的数据承接规则。
- 新建、升级和重复初始化时的预期结果。

### SchemaSnapshot

当前实例迁移前后的只读摘要，由 PostgreSQL 元数据查询产生：

- schema、表和字段名称。
- 数据类型、可空性和默认值。
- 主键、唯一约束、外键和索引定义。
- 各相关表行数。
- 必填字段的空值或空字符串计数。

快照不包含消息正文、记忆正文、密码摘要、session token 或其他业务载荷。

## 核心接口

### Store 初始化约定

所有 PostgreSQL store 保持现有构造入口，并遵循统一时序：

```python
def _init_schema(self) -> None:
    create_current_tables_if_missing()
    migrate_recognized_legacy_shapes()
    create_or_repair_required_indexes()
    commit()
```

迁移辅助方法保持 store 私有，不增加跨领域的通用业务迁移器。方法必须使用当前连接和同一事务；失败时由调用方收到包含 store/表/字段上下文的异常。

### 结构识别约定

store 通过 `information_schema.columns`、`information_schema.table_constraints` 和 PostgreSQL catalog 查询当前 schema，不依赖固定的 `public` schema。所有测试 DSN 使用独立 `search_path` 时必须得到相同结果。

### 数据保全断言

删除旧列或收紧约束前执行集合式验证：

```text
目标字段无无效空值
旧字段的有效值均已映射
迁移前后业务行数一致
```

断言失败时中止事务，不删除旧结构。

### 审计报告接口

最终报告为仓库内 Markdown 工件，由审计矩阵、store 契约核对表、当前实例前后快照摘要和测试证据组成。它是本次工作的审计记录，不成为应用启动依赖。

## 模块设计

### 公共 PostgreSQL 基础模块

**职责：** 解析 DSN、建立 dict-row 连接、提供参数风格转换，并提供基于当前 schema 的只读字段盘点辅助能力。

**对外接口：**

```python
def table_columns(conn: Any, table_name: str) -> set[str]: ...
```

该接口只读取 `information_schema`，不执行 DDL。表名只能由代码内常量传入；业务 store 仍负责决定添加、迁移或删除哪些字段。

**依赖：** psycopg、当前连接的 `search_path`。

### 会话存储

**职责：** 管理 `sessions` 与 `messages`，完成旧会话身份字段到 `active_agent` 的一次性升级。

**迁移行为：** 同时支持旧表仅含旧字段、半迁移表同时含新旧字段、当前表仅含目标字段。半迁移时优先保留已有非空目标值，否则承接旧值，完成校验后删除旧字段。

**验证重点：** 旧数据保留、外键消息保留、新会话写入、二次初始化幂等。

### Telegram 与飞书网关存储

**职责：** 管理会话映射、幂等事件/offset 与 outbox。

**迁移行为：** 两个 outbox 对相同功能字段采用一致的增量升级策略。新增的可空字段直接添加；带默认值的必填字段通过数据库默认值回填。不得改变已有消息状态、尝试次数或正文。

**验证重点：** 从不含文档消息字段的旧 outbox 升级后，文本消息仍可读取，文档消息可写入；重复初始化不改变行数。

### Web 鉴权、记忆归档、Trace 与工具结果存储

**职责：** 分别拥有其现有业务表。

**迁移行为：** 对建表、读写字段和当前实例结构做闭环核对。只有仓库阶段代码、调用方或可构造旧结构能够证明字段演进时才增加迁移；无法推导安全值的必填字段不自动补造，转入待决报告。

**验证重点：** 新库结构完整、当前结构重复初始化安全、查询/写入字段集合闭合、索引与外键存在。

### 生产兼容入口

**职责：** 核对子代理 payload 兼容字段、工具 schema 导入别名、AgentSpec profile、消息旧格式和 scoped memory legacy store。

**处理规则：** 有测试、公开导入、协议解析或明确运行时用途的保留；只有调用方扫描为零且当前行为测试能证明无影响时才移除。模型 fallback、离线 fallback、检索策略 fallback 不属于重构遗留。

### 测试与审计报告

**职责：** PostgreSQL 临时 schema 测试、静态扫描证据、当前实例快照与最终分类记录。

**输出：** store 专项测试及 `audit-report.md`。测试数据库只使用临时 schema；当前实例验证产生的临时业务记录必须在验证结束前删除。

## 模块交互

```text
生产代码扫描 ───────────────┐
                           ▼
当前 store SQL ──→ 结构契约核对 ──→ 候选项分类
当前实例快照 ───────────────┘          │
                                      ├─ 保留/排除 ──→ 审计报告
                                      │
                                      └─ 真实遗留
                                           │
                                           ▼
                                  store 私有事务迁移
                                           │
                    ┌──────────────────────┼──────────────────────┐
                    ▼                      ▼                      ▼
                 新库测试               旧库升级测试            幂等测试
                    └──────────────────────┼──────────────────────┘
                                           ▼
                                  当前实例数据保全检查
                                           ▼
                                  初始化与隔离读写复核
                                           ▼
                                        审计报告
```

## 文件组织

```text
runtime/
├── db.py                              — 当前 schema 的公共只读元数据辅助
├── sessions/session_store.py          — 会话身份迁移
├── trace/index_store.py               — trace schema 契约与必要迁移
└── tooling/result_store.py            — 工具结果 schema 契约与必要迁移
gateway/
├── telegram/store.py                  — Telegram outbox 升级
└── feishu/store.py                    — 飞书 outbox 升级
memory/archive_store.py                — 归档 schema 核对与必要迁移
web/auth_store.py                      — 鉴权 schema 核对与必要迁移
tests/
├── postgres_utils.py                  — 临时 schema 测试辅助
├── test_session_store_incremental.py  — 会话迁移三态测试
├── test_telegram_gateway.py           — Telegram 旧表升级测试
├── test_feishu_gateway.py             — 飞书旧表升级测试
├── test_web_auth.py                   — 鉴权结构测试
├── test_memory_lifecycle_archive.py   — 归档结构测试
├── test_run_trace.py                  — trace 结构测试
└── test_tool_result_store.py          — PostgreSQL 工具结果结构测试
specs/refactor-legacy-audit/
├── spec.md
├── plan.md
├── task.md
├── checklist.md
└── audit-report.md                    — 最终审计证据
```

文件清单是审计上限，不代表每个生产文件都必须修改。若核对结果证明结构闭合，对应文件只记录“保留/无需修改”及证据。

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| schema 所有权 | 各 store 自主管理 | 避免集中迁移器与运行时建表形成双重真相 |
| 公共复用 | 仅抽取只读 `table_columns` | 消除重复元数据 SQL，同时不让公共层决定业务 DDL |
| schema 范围 | 使用 `current_schema()` | 兼容生产 `public` 与测试临时 `search_path` |
| 迁移方式 | store 初始化事务内的集合式 SQL | 保证原子性、启动可达和数据量可控 |
| 旧列删除 | 先承接并验证，再删除 | 防止数据丢失和半迁移约束故障 |
| 未知必填字段 | 不自动猜值 | 无法证明业务语义时，报告风险比制造错误数据安全 |
| 兼容层判定 | 测试/协议/调用方证据优先 | 避免把正常兼容和容错机制误删 |
| 当前实例修改 | 测试覆盖 + 前置数据检查后执行 | 将生产风险限制在已验证迁移 |
| 审计产物 | Markdown 报告，不接入启动路径 | 保持运行时简单且便于人工复核 |
