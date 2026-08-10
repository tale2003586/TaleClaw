# TaleClaw Runtime Architecture Closure Result

Date: 2026-08-04

## Scope and audit findings

The audit found overlapping turn entry points, an outer AgentLoop that also
implemented application routing, WorkingMemory execution caches duplicated in
TaskState, compaction coupled to semantic StatePatch application, parallel tool
governance metadata, unrestricted reflection recovery, and no explicit model
Thinking capability contract. The audit baseline is recorded in
[`runtime-pruning-audit.md`](runtime-pruning-audit.md).

## Removed and migrated paths

Deleted from the formal production path:

* `runtime/agent_loop.py`, `runtime/app_runtime.py` and `runtime/bootstrap.py`;
  their application responsibilities moved to `applications/turn_coordinator.py`,
  `applications/app_runtime.py` and `applications/bootstrap.py`.
* `runtime/working_memory.py` and all new-task WorkingMemory writes.
* `applications/coding/compaction.py`; event compaction now lives beside the
  independent ContextSnapshot manager.
* `tools/governance.py` and the old parallel governance API.
* `Runtime.run_turn` and `_run_turn`; callers and tests use `Runtime.run`.

Retained only as one-way migration readers are legacy WorkingMemory and old
CodingContextState payloads in TaskState migration code. They are never written
back by new runs. The obsolete context-state and semantic-compaction flags and
the `compaction_persister` compatibility parameter were removed.

## Final runtime and state model

The production chain is:

```text
Inbound adapter -> AppRuntime -> TurnCoordinator -> ApplicationRouter
  -> Chat or CodingApplication -> Runtime.run
  -> AgentRunner -> ReasoningLoop -> ContextBuilder/Model/ToolExecutor
```

`Session` owns conversation transport and immutable events. `TaskState` owns
cross-run task meaning. `RunExecutionState` owns per-run budgets, fingerprints,
usage, recovery and stop state. Trace/Event is immutable operational evidence.
`ContextSnapshot` owns compressed context. Runtime has no Coding, BUS, gateway
or Web fields and no product-layer imports. Ordinary bot chat does not create
TaskState, WorkingMemory or CodingContextState.

## ContextSnapshot and compaction

`ContextSnapshotManager` implements content-addressed, idempotent
`PREPARED -> ACTIVE -> archive` activation. Prepared rows are invisible to
prompts. Activation failure does not archive; archive failure leaves the active
snapshot usable and is recoverable after restart. The persisted load path now
also restores the archive boundary from an archived active snapshot.

`EventCompactor` is independent of TaskState patches. It uses bounded normal,
repair and chunked provider attempts followed by deterministic fallback. A
snapshot failure does not change TaskState; a StatePatch failure cannot roll
back a valid snapshot or repeat a tool action.

## Recovery and stop decisions

`RecoveryJudge` is a single no-tool diagnostic call. `RecoveryController` allows
one judge and one corrected attempt for a read-only/idempotent incident. Any
side-effecting or non-idempotent duplicate stops immediately, and a repeated
incident stops with `recovery_exhausted`. Incident fingerprints include the
tool/arguments, error type, result hash and TaskState version; the run-wide
judge budget is capped at two. Stop paths use the standard
`StopReason`/`StopDecision` values rather than free-form session metadata.

## ToolSpec convergence

Registry, policy, catalog, executor and recovery consume the same `ToolSpec`
instance. The instance carries schema, handler, modes, risk, idempotence,
side-effect/state effect, injection, session scope, admin permission, audit tag
and runtime parameters. Plugins register `ToolSpec` directly. The built-in
factory's small name-to-default mapping is used only while constructing those
specs; policy, catalog, executor and recovery do not consult a second metadata
table.

## Thinking implementation

The formal field is `thinking_enabled: bool`, default false. `ModelProfile`
adds explicit `supports_thinking` and `thinking_param` declarations; invalid
support declarations are rejected. OpenAI-compatible non-streaming, streaming,
Responses and tool-call requests add only the declared provider parameter when
enabled. Web chat and stream APIs validate the optional field, return 400 when
unsupported, and expose capability through `/api/health`. React disables the
control when unsupported and does not display hidden reasoning.

There was no pre-existing model CRUD persistence surface in this repository;
environment/JSON model profile loading remains the configuration authority.
Missing Thinking fields in old profiles default safely to false.

## Verification

Executed checks:

* `.venv/bin/python -m pytest --collect-only -q`: 681 tests collected, no
  collection errors.
* no-PostgreSQL suite (the documented 17-file exclusion): **517 passed** in
  23.25 seconds.
* New architecture tests: 34 passed, covering snapshots, recovery, ToolSpec,
  Thinking and static runtime boundaries.
* `npm test -- --run`: **26 passed**.
* `npm run build`: passed; generated hashed assets are current build output.
* `npm run lint`: passed.
* Full `.venv/bin/python -m pytest -q --tb=short -x`: **129 passed, 1 skipped**
  before the first PostgreSQL connection failure in
  `test_feishu_gateway.py`. PostgreSQL is unavailable on this machine; no
  production workaround was added.
* `git diff --check`: passed after final code and documentation changes.

The final tracked-worktree `git diff --stat` is **124 files changed, 1,120
insertions, 8,350 deletions**. The worktree also contains the newly added
untracked closure modules, tests and architecture documents listed by
`git status --short`; generated hashed frontend assets are expected build
outputs.

## Compatibility inventory

| Item | Old format/path | New authority | Reason retained | Removal condition |
| --- | --- | --- | --- | --- |
| WorkingMemory | `metadata.working_memory` | TaskState/events/run state | Read old persisted sessions once | After supported session retention window |
| Coding context state | `metadata.coding_context_state` | ContextSnapshot + renderer | Read old checkpoints | After supported session retention window |
| Runtime entry | `run_turn`, `_run_turn` | `Runtime.run` | None | Removed in this closure |
| Session run metadata | `active_run_id`, stop strings and counters | RunState/RunExecutionState/trace | Existing rows are ignored | No new writes; delete after retention |
| Model profiles | Missing Thinking fields | `false` default | Old env/JSON compatibility | Permanent safe read default |

## Remaining risks

PostgreSQL-dependent persistence and gateway tests could not execute locally.
The configured external model providers were not called. The existing legacy
migration readers still require a documented retention window before deletion.
Those are operational follow-ups, not alternate runtime paths.

## Future extension points

Future Sandbox resource references should be injected by an Application
contributor and governed by ToolSpec/Policy; no new Runtime import is needed.
A future TaskPlan should enter through TaskState reducers/events while keeping
per-run scheduling and limits in RunExecutionState. TaskPlan and a new Sandbox
lifecycle were explicitly not implemented in this closure.
