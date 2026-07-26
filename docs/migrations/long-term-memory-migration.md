# Long-term Memory Migration Runbook

本 Runbook 用于把 legacy Markdown/JSON 长期记忆迁移到 PostgreSQL，并从 PostgreSQL 重建 Qdrant semantic index。所有命令先在隔离环境执行；默认命令是 dry-run，不删除源文件、旧表或旧 collection。

## 1. Upgrade

1. 确认 PostgreSQL 与测试 Qdrant 健康，记录当前提交：

   ```bash
   git status --short --branch
   python -m pytest -q tests/test_postgres_store_schemas.py
   ```

2. 部署包含 `PostgresMemoryRepository` 的代码。首次构造 Repository 会以当前幂等初始化模式创建：

   - `memory_items`
   - `memory_evidence`
   - `memory_index_outbox`
   - `memory_schema_versions`

3. 先保持以下开关关闭：

   ```dotenv
   SEMANTIC_MEMORY_ENABLED=0
   SEMANTIC_MEMORY_WRITE_ENABLED=0
   SEMANTIC_MEMORY_READ_ENABLED=0
   SEMANTIC_MEMORY_CONTEXT_ENABLED=0
   ```

4. 重复启动一次，确认 schema 初始化幂等。升级阶段只新增表，不修改或删除 legacy 文件。

## 2. Dry-run

先保存源文件的路径、大小与校验值：

```bash
find memory -maxdepth 1 -type f -print0 | sort -z | xargs -0 sha256sum > /tmp/taleclaw-memory-before.sha256
python scripts/migrate_long_term_memory.py \
  --source-root memory \
  --user-id local \
  --include-candidates \
  --report-path /tmp/taleclaw-memory-dry-run.json
```

未提供 `--apply` 时始终是 dry-run。检查报告中的 `imported`、`candidate`、`skipped`、`review`、`duplicate`、`conflict` 和 `failed`。`failed` 必须先解决；`review` 必须人工判定，不能自动改为 active。

映射规则：

| 来源 | 处置 |
|---|---|
| `MEMORY.md` bullet | user-scoped active legacy fact |
| `PENDING.json` candidate | user-scoped candidate |
| `PENDING.md` | 与 JSON 对照，review，不自动 active |
| `SELF.md` / `NOW.md` | review，不自动导入用户事实 |
| `HISTORY.md` / `RECENT_CONTEXT.*` | skip semantic |
| Coding conclusion | 通过运行时统一 proposal，优先 project/workspace scope |

Dry-run 后再次校验源文件：

```bash
sha256sum --check /tmp/taleclaw-memory-before.sha256
```

## 3. Apply

只在 dry-run 报告获得人工批准后执行：

```bash
python scripts/migrate_long_term_memory.py \
  --source-root memory \
  --user-id local \
  --include-candidates \
  --apply \
  --checkpoint /tmp/taleclaw-memory-checkpoint.json \
  --report-path /tmp/taleclaw-memory-apply.json
```

Importer 使用 source、owner、kind 与 normalized content 生成幂等键。中断后用相同 checkpoint 重跑；重复项必须报告为 `duplicate`，不能产生第二条 active memory。Apply 仍不会修改或删除源文件。

应用开关按顺序启用，每一步完成 smoke test 后再继续：

1. `SEMANTIC_MEMORY_ENABLED=1`
2. `SEMANTIC_MEMORY_WRITE_ENABLED=1`
3. `SEMANTIC_MEMORY_READ_ENABLED=1`
4. `SEMANTIC_MEMORY_CONTEXT_ENABLED=1`

禁止 PostgreSQL 与 Markdown 双向同步。迁移期只有 PostgreSQL 主写；Markdown 只能由 exporter 单向生成。

## 4. Verify

1. 重复 importer，确认新增数为零、duplicate 数与已导入输入一致。
2. 先 dry-run semantic index rebuild：

   ```bash
   python scripts/rebuild_memory_index.py
   ```

3. 使用新的空 collection 名称，显式 apply：

   ```bash
   SEMANTIC_MEMORY_QDRANT_COLLECTION=taleclaw_semantic_memory_v1 \
     python scripts/rebuild_memory_index.py --apply
   ```

4. 运行定向验证：

   ```bash
   python -m pytest -q \
     tests/test_legacy_memory_importer.py \
     tests/test_memory_index_rebuild.py \
     tests/test_memory_markdown_export.py \
     tests/test_semantic_memory_retrieval.py \
     tests/test_episodic_history_scope.py
   ```

5. 用 Session A 写显式偏好；Session B 应命中 semantic memory，但不得命中 A 的原始 turn。
6. 比较 PostgreSQL active ID/version 集合、rebuild selected/indexed 数和 Qdrant point payload。payload 不应包含 Evidence 或完整历史。
7. 重新执行迁移前 checksum 检查，确认源文件未被改变。

## 5. Rollback

回滚只切换读写装配，不删除已经提交的事实：

```dotenv
SEMANTIC_MEMORY_CONTEXT_ENABLED=0
SEMANTIC_MEMORY_READ_ENABLED=0
SEMANTIC_MEMORY_WRITE_ENABLED=0
SEMANTIC_MEMORY_ENABLED=0
```

- 暂停 outbox synchronizer；保留 pending/retry 事件，恢复后继续 drain。
- Context 可临时切回 legacy reader，但不得把 legacy 结果反向覆盖 PostgreSQL。
- Qdrant rebuild 失败时继续使用旧 collection；不要删除失败的新 collection，先保留证据并调查。
- Apply batch 如需逻辑撤销，应按导入报告中的 memory ID 逐条 revoke；不执行物理 DELETE。
- Schema downgrade 等价操作是停用新装配。当前阶段不 DROP 新表。

## 6. Cleanup

只有在以下条件全部满足后，才能另开审批进行永久清理：

- importer apply 与重复执行报告已保存；
- semantic/episodic/working Context 验收通过；
- Qdrant 可从 PostgreSQL 重建；
- legacy 文件 checksum 与只读归档已保存；
- 观察期内没有回滚；
- 完整测试通过。

本次重构只停止生产调用并标记 legacy adapter，不自动删除：

- `MEMORY.md`、`PENDING.*`、`HISTORY.md`、`RECENT_CONTEXT.*`、SELF、NOW；
- PostgreSQL `memory_archive`；
- 旧 Qdrant collection 或 user-scoped points；
- 任何 Session 原始消息。

永久删除文件、表、collection 或审计记录需要单独的范围、备份、保留期和审批。
