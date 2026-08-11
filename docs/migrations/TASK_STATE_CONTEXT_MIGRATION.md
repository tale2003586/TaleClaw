# TaskState Legacy Migration

TaskState is the only mutable task-progress authority. New sessions write only
the `task_state` v2 envelope. They never write `working_memory`,
`coding_context_state`, or `memory_root`.

## Accepted Legacy Input

`runtime/task_state/legacy.py` is the sole parser for persisted legacy task
payloads:

- `session.metadata.working_memory`;
- `session.metadata.coding_context_state`;
- pre-v2 flat `task_state` payloads.

These values are migration input only. They are not rendered into prompts,
updated during normal execution, or used as a second task authority.

## Migration Transaction

`ensure_task_state_core()` performs the migration when a supported legacy
session is opened:

```text
read legacy payload
  -> convert to TaskStateCore
  -> save current task_state envelope
  -> remove working_memory/coding_context_state keys
  -> append migration checkpoint
```

Before writing, the service snapshots metadata, event log, checkpoints, archive
boundary, and timestamp. If any write or checkpoint step fails, it restores the
entire snapshot and propagates the error. No partial migration or legacy/current
double write is allowed.

Unknown old coding details are retained under the current envelope's migration
extension so user data is not discarded. Runtime code does not consume that
extension as current state.

## Verification

Run:

```bash
python -m pytest -q tests/test_task_state_core.py tests/test_shared_runtime_task_state.py
```

For stored sessions, verify that opening and saving each legacy session creates
a v2 `task_state` envelope and a migration checkpoint, while the two old keys
disappear. Back up the session database before any bulk migration. Do not delete
or rewrite database rows outside the application transaction.

## Removal Condition

Delete `runtime/task_state/legacy.py` only after all supported persisted
sessions have either:

1. been opened and checkpointed under TaskState v2;
2. been migrated by an audited bulk job; or
3. expired under the product's documented retention policy.

Until then the adapter remains read-only at the compatibility boundary. New
code must not import its schema constants or emit its payloads.
