# 生产代码重构遗留审计与修复 Tasks

## 文件清单

| 操作 | 文件 | 职责 |
|---|---|---|
| 修改 | `runtime/db.py` | 当前 schema 字段盘点辅助 |
| 核对/按证据修改 | `runtime/sessions/session_store.py` | 会话身份迁移 |
| 核对/按证据修改 | `gateway/telegram/store.py` | Telegram schema 升级 |
| 核对/按证据修改 | `gateway/feishu/store.py` | 飞书 schema 升级 |
| 核对/按证据修改 | `web/auth_store.py` | Web 鉴权 schema |
| 核对/按证据修改 | `memory/archive_store.py` | 记忆归档 schema |
| 核对/按证据修改 | `runtime/trace/index_store.py` | trace 索引 schema |
| 核对/按证据修改 | `runtime/tooling/result_store.py` | 工具结果 schema |
| 修改 | 对应 `tests/test_*.py` | 新库、旧库、幂等与行为回归 |
| 新建 | `specs/refactor-legacy-audit/audit-report.md` | 分类、快照和验证证据 |

## T1：建立完整候选项与 Store 契约矩阵

**文件：** `specs/refactor-legacy-audit/audit-report.md`

**依赖：** 无

**步骤：**

1. 扫描生产 Python 中的建表、DDL、旧命名、兼容注释和 compatibility API。
2. 列出八个数据库相关模块的表、目标字段、约束、索引、读取和写入字段。
3. 将每个候选项初分为真实遗留、明确保留、范围排除或待决。
4. 记录每个分类的代码、测试、协议或当前实例证据。

**验证：** 报告覆盖全部扫描命中和全部 PostgreSQL store，不存在无分类候选项。

## T2：统一安全的当前 Schema 字段盘点

**文件：** `runtime/db.py`、相关数据库测试

**依赖：** T1

**步骤：**

1. 增加只读 `table_columns(conn, table_name)`。
2. 查询限定为 `current_schema()`，表名作为查询参数。
3. 替换 store 内重复且等价的字段查询，不移动业务 DDL 决策。
4. 在临时 search path 下验证返回目标 schema 字段而非 `public` 同名表。

**验证：** 公共数据库辅助测试通过；生产代码不再复制同一段字段盘点 SQL。

## T3：完成会话身份迁移三态闭环

**文件：** `runtime/sessions/session_store.py`、`tests/test_session_store_incremental.py`

**依赖：** T2

**步骤：**

1. 核对旧表、半迁移表和当前表的初始化路径。
2. 验证旧值承接、已有目标值优先级、非空约束和旧列删除顺序。
3. 增加全新库、旧库、半迁移库和二次初始化测试。
4. 验证 messages 外键行及会话行数保持不变。

**验证：** 会话 store 专项测试全部通过，测试后只保留 `active_agent`。

## T4：补齐 Telegram outbox 增量升级

**文件：** `gateway/telegram/store.py`、`tests/test_telegram_gateway.py`

**依赖：** T2

**步骤：**

1. 以契约矩阵核对旧 outbox 与当前读写字段。
2. 将字段识别改用公共只读辅助。
3. 对有演进证据且可安全添加的字段执行幂等升级。
4. 构造旧表与文本消息，升级后验证原行、文档消息写入和二次初始化。

**验证：** Telegram 专项测试通过，旧消息行数和状态不变。

## T5：补齐飞书 outbox 增量升级

**文件：** `gateway/feishu/store.py`、`tests/test_feishu_gateway.py`

**依赖：** T2

**步骤：**

1. 对齐 Telegram/飞书共同 outbox 功能字段的升级能力。
2. 为旧表缺失的文档类型、路径和标题字段增加安全幂等迁移。
3. 构造旧表与文本消息，升级后验证原行、文档消息写入和二次初始化。
4. 保持事件幂等、会话映射和发送状态逻辑不变。

**验证：** 飞书专项测试通过，旧文本消息可读且新文档消息可写。

## T6：核对 Web 鉴权与记忆归档

**文件：** `web/auth_store.py`、`memory/archive_store.py` 及对应测试

**依赖：** T1、T2

**步骤：**

1. 闭环比对建表、读取、写入、唯一约束、外键和索引。
2. 结合调用方和现有结构判断是否存在可证明的字段演进。
3. 仅对可安全推导的数据增加迁移；未知必填语义写入待决报告。
4. 为发现的每项真实遗留增加三态测试；无遗留时增加或保留当前结构契约测试。

**验证：** Web auth 与 memory archive 专项测试通过；报告包含明确结论。

## T7：核对 Trace 与 PostgreSQL 工具结果存储

**文件：** `runtime/trace/index_store.py`、`runtime/tooling/result_store.py` 及对应测试

**依赖：** T1、T2

**步骤：**

1. 比对大字段集合的建表、upsert、select 和索引。
2. 检查新增 nullable/default 字段是否缺少旧表升级路径。
3. 对有演进证据的安全字段增加幂等迁移；不可安全回填项转入待决。
4. 覆盖新表、代表性旧表和重复初始化，并验证 trace step 外键与工具结果内容哈希不变。

**验证：** trace 与 tool result 专项测试通过；字段集合和 SQL 参数数量闭合。

## T8：分类生产兼容入口

**文件：** 相关生产模块、现有调用方测试、`audit-report.md`

**依赖：** T1

**步骤：**

1. 核对子代理 payload compatibility、工具导入别名、AgentSpec profile、旧消息格式和 scoped memory legacy store。
2. 使用调用方扫描、测试和明确注释确认保留依据。
3. 只删除零调用且无协议依据、并有行为测试覆盖的真实残留。
4. 将模型/检索/离线 fallback 和不同领域 `mode` 标记为合法语义。

**验证：** 相关行为测试通过；每个兼容候选均有保留或移除证据。

## T9：审计并对齐当前 PostgreSQL 实例

**文件：** `audit-report.md`（数据库仅执行已验证迁移）

**依赖：** T3–T7

**步骤：**

1. 记录相关表的结构、约束、索引、行数和必填字段无效值计数。
2. 对每个待执行迁移先运行数据保全查询。
3. 通过对应 store 初始化执行已测试迁移；不直接执行未经测试的临时 DDL。
4. 重新采集摘要，执行临时 schema 或可清理记录的隔离读写验证。
5. 删除验证记录并确认业务行数符合预期。

**验证：** 当前实例所有 store 可初始化，迁移前后业务数据保全，结构符合契约。

## T10：专项与全量回归

**文件：** 测试与 `audit-report.md`

**依赖：** T3–T9

**步骤：**

1. 运行数据库 store 专项测试。
2. 运行兼容入口相关测试。
3. 运行完整测试套件。
4. 对环境依赖型跳过或失败单独分类，不掩盖真实回归。
5. 扫描凭证模式和无关文件改动。

**验证：** 专项测试全通过；全量测试无本次引入的失败；报告记录实际数量。

## T11：完成审计报告与验收映射

**文件：** `audit-report.md`、`checklist.md`

**依赖：** T10

**步骤：**

1. 将候选项最终归入已修复、明确保留、范围排除、待人工决策。
2. 补充每个 store 的契约核对结果。
3. 记录当前实例前后摘要与测试证据。
4. 按 checklist 逐项执行并标记实际结果。

**验证：** AC1–AC9 均能在报告中找到对应证据，未通过项有明确原因和后续方案。

## 执行顺序

```text
T1 → T2 → T3 ─┐
          ├→ T4 ─┤
          ├→ T5 ─┤
          ├→ T6 ─┼→ T9 → T10 → T11
          └→ T7 ─┘
T1 ─────────→ T8 ┘
```
