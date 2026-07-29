# TaskState Context Architecture Tasks

## File List

| Operation | Files | Responsibility |
|---|---|---|
| Add | `runtime/context/artifacts.py`, `long_content.py`, `events.py`, `dynamic_budget.py` | New runtime primitives |
| Add | `applications/coding/task_state.py`, `compaction.py` | Authoritative state and compaction |
| Modify | `runtime/sessions/session.py`, `session_store.py` | Event/checkpoint persistence and recovery |
| Modify | `applications/coding/context_state.py`, `handoff.py`, `runner.py` | Compatibility renderer, deduplication, offloading |
| Modify | `runtime/context/builder.py`, `runtime/execution/reasoning_loop.py`, `config.py` | Prompt integration, flags, hard guard |
| Add/Modify | `tests/test_*context*`, architecture docs | Verification and migration documentation |

## Ordered Tasks

- T1: Add content-addressed ArtifactStore and token-first LongContentDetector. Verify range/search/outline/sample and 500k input offload tests.
- T2: Add typed stable ContextEvent log, transaction-safe grouping, active event selection, and persistence schema. Verify append-only/restart tests.
- T3: Add TaskState schemas and deterministic runtime field maintenance. Verify phase/action/finding/evidence rules.
- T4: Add legacy WorkingMemory/CodingContextState migration. Verify idempotence and hypothesis downgrade.
- T5: Add deterministic extractor, semantic StatePatch compactor, validator, and reducer. Verify invented evidence and illegal transitions fail.
- T6: Add checkpoint/compaction transaction and recovery. Verify every injected failure leaves the boundary unchanged.
- T7: Remove current request from handoff and externalize large coding requests/results. Verify the body exists once and request appears once.
- T8: Replace fixed coding prompt compaction with TaskState renderer and token-selected groups. Verify latest user priority and complete tools.
- T9: Add provider-aware budget and final hard guard. Verify different windows and blocked over-limit invocation.
- T10: Add metrics, migration/architecture docs, broad tests, diff checks, and manual acceptance run.

## Execution Order

`T1 + T2 + T3 -> T4 + T5 -> T6 -> T7 + T8 -> T9 -> T10`

