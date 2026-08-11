# TaskState And ContextSnapshot Architecture

Task progress and context compaction are separate facts with separate owners.

## Ownership

| Concern | Owner | Persistence |
|---|---|---|
| Objective, constraints, progress, pending actions, blockers, completion basis | `TaskStateCore` | current `task_state` session envelope |
| Coding-specific rendering details | coding TaskState extension | same current envelope |
| Immutable conversation and tool facts | Session event log | session repository |
| Compacted summary and covered event boundary | `ContextSnapshot` | snapshot repository |
| Per-run counters, cancellation, recovery and stop decision | `RunExecutionState` | run/trace lifetime |
| Large content body | Artifact store | content-addressed artifact plus references |

`applications/coding/context_state.py` builds a read-only coding view from these
owners. It does not persist a second objective, plan, blocker list, or progress
ledger. A snapshot may summarize TaskState facts but cannot mutate them.

## Run Lifecycle

TaskState is optional. Coding supplies `TaskStateRunObserver` through
`RuntimeExtensions.run_observers`. `ReasoningLoop` emits a generic
`StopDecision`; the observer maps completed, cancelled, failed, or blocked
decisions into TaskState and returns the resulting version. Chat runs without
that observer and do not create TaskState.

Tool handlers update TaskState through validated optimistic patches. Terminal
states cannot be reopened, completion requires evidence/basis and no pending
actions or blockers, and stale base versions are rejected.

## Snapshot Lifecycle

```text
REQUESTED / GENERATING
        -> PREPARED
        -> ACTIVE
        -> archive boundary advanced

        -> FAILED_RETRYABLE / FAILED_FINAL
```

Only an active snapshot is eligible to advance the event archive boundary.
Snapshot identity includes the covered event range and source TaskState version,
so retrying the same compaction is idempotent. Failure preserves the prior
active snapshot and original events. Deterministic fallback retains objective,
constraints, pending work, blockers, evidence/artifact references, and recent
covered events when model summarization is invalid or unavailable.

## Legacy Input

Old `working_memory`, `coding_context_state`, and flat TaskState payloads are
handled only by `runtime/task_state/legacy.py`. The adapter converts them to the
current envelope, checkpoints the migration, and removes old keys atomically.
They are never rendered or updated as current runtime state. See
`docs/migrations/TASK_STATE_CONTEXT_MIGRATION.md` for the removal condition.

## Runtime Configuration

TaskState and ContextSnapshot are architecture contracts, not parallel feature
flags. Current tunable controls are limited to artifact offload, dynamic prompt
budget ratios, context section budgets, and provider/model limits. Removed
WorkingMemory, CodingContextState, TaskState-context, and semantic-compaction
flags have no compatibility aliases.
