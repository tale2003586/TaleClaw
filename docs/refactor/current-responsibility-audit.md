# Current Responsibility Audit

> Historical pre-pruning inventory. Current ownership is documented in
> `../architecture/SYSTEM_ARCHITECTURE.md` and `product-capability-matrix.md`.

Audit of HEAD `03a1580` before slimming changes.

## Runtime Inventory

| Module | Primary responsibility | Inputs / outputs | State owned/read/written | Prompt / tools / persistence | Disable/delete/overlap |
|---|---|---|---|---|---|
| `runtime/runtime.py` | Public `Runtime.run` facade and turn callbacks | `AgentSpec`, input, `RunContext` -> `RunResult` | Per-run execution state; reads session | Adds tool catalog; invokes lifecycle | Core; catalog overlaps schemas |
| `runtime/execution/reasoning_loop.py` | context -> model -> tools -> state -> stop | context bundle/model/tool results | Run execution counters and deterministic checkpoints | Sends final messages/tools; trace only persists observations | Core, oversized; special policies remain P1 |
| `runtime/context/builder.py` | Assemble and budget model context | session/profile/providers -> bundle/report | Cache and context metrics | Produces system, runtime guidance and structured sections | Core; static guidance duplicates AgentSpec/tool descriptions |
| `runtime/context/providers.py` | History, memory, retrieval provider boundaries | session/current request -> typed sections | No durable state | One memory provider and one retrieval provider | Core; provider split is already authoritative |
| `runtime/task_state/` | Shared semantic long-task state | patches/events -> `TaskStateCore` | Owns objective/progress/blockers/evidence/completion | Session metadata + checkpoint persistence; rendered context | Optional for task modes; authoritative task progress |
| `runtime/sessions/` | Conversation/event/checkpoint persistence | messages/events -> session | Owns transcript and checkpoint records | Postgres persistence | Core conversation state |
| `applications/coding/context_state.py` | Coding context projection and compaction metadata | events/task state -> render view/snapshot | Owns compaction generation/boundary/summary only | Checkpoint-derived context | Optional coding; does not own progress |
| `applications/coding/task_state.py` | Rich coding state plus legacy migration | old payload/event history -> shared state | Reads legacy `working_memory` and old coding state once; writes task state | Migration checkpoint compatibility | Keep minimal adapter; migration redesign is P2 |
| `applications/bootstrap.py` | Build product runtime and extensions | environment -> `AppRuntime` | Process-level service graph | Currently constructs memory lifecycle even when semantic memory is off | P0 candidate: make lifecycle truly optional |
| `memory/store.py` / `scoped_store.py` | Legacy Markdown memory and task-local files | sections/text -> files | Legacy durable files | `SELF/MEMORY/NOW/PENDING`, history/recent | Migration/task-local compatibility, not semantic truth |
| `memory/repository.py` / `postgres_repository.py` | Semantic durable memory source of truth | commands -> `MemoryItem` | Owns semantic items/evidence/transitions/outbox | Postgres authoritative store | Optional LongTermMemory core |
| `memory/semantic_retrieval.py` | Query/rank active semantic memory | scope/query -> ranked hits/render | Reads repository and index | Injected only via `MemoryContextProvider`; recall tool also calls it | Optional; auto top-k vs explicit deep pull |
| `memory/lifecycle.py` | Derived post-turn history/candidate/archive work | completed turn -> derived records | Writes legacy history/recent, archive, vector index, proposals | Background by default at HEAD | Optional and too eager |
| `memory/command_service.py` | Governed semantic writes/transitions/conflicts | proposal/transition -> repository item | Repository is owner; service validates outcomes | Used by memorize/coding promotion | Optional closed write path |
| `memory/evolution.py` | Generate relation/evolution proposals | note + matches -> proposals | None | No runtime caller or consumer | M4 dead experiment |
| `memory/notes.py` relation types | Alternate note/link model | semantic item -> compatibility note | None authoritative | Link types have no production caller/persistence | Relation portion is M4 dead |
| `tools/spec.py` / registry / policy / executor | Tool definition -> registration -> visibility -> execution | `ToolSpec`, session, mode | Session unlock list only | Schemas sent to model; hooks enforce runtime rules | Core; one governance owner exists |
| `agents/subagent/runner.py` | Isolated child execution | bounded prompt/type -> result | Isolated session and child execution state | Manually rebuilds `Runtime`; filtered registry | P1: derive via `Runtime.fork` while preserving registry |

## State Ownership Matrix

| Fact | Authoritative owner | Compatibility/read-only mirrors | Finding |
|---|---|---|---|
| Conversation messages | `Session.messages` and session event store | Compaction snapshots summarize ranges | One owner |
| Task objective/progress/blockers/evidence | `TaskStateCore` / coding `TaskState` projection | legacy `working_memory` and `coding_context_state` are migration inputs only | One current writer |
| Compaction summary/boundary | `ContextSnapshot` / checkpoint store | `CodingContextState.last_compaction` rendering metadata | No task-progress ownership |
| Recent active context | Current session events and rebuilt coding view | Context metrics in metadata | Rebuildable |
| Recovery | Session checkpoints + task-state version/checksum | Legacy payload migration checkpoint | Compatibility remains intentionally read-only |
| Runtime counters | `RunExecutionState` | Trace reports | Trace is observational |
| Durable semantic memory | `LongTermMemoryRepository` (`PostgresMemoryRepository`) | vector index is derived; legacy Markdown has migration adapter | Semantic path has one intended owner, legacy path still operational when enabled |

## WorkingMemory Field Matrix

No `WorkingMemory` runtime class or writer remains. Only migration readers exist.

| Legacy field | Meaning | Current owner | Persistence/rebuild | Decision |
|---|---|---|---|---|
| goal/objective | task objective | TaskState objective | checkpointed | migration read only |
| progress/completed | completed work | TaskState completed/progress | checkpointed | migration read only |
| next_step/next_action | current focus | TaskState current focus/remaining | checkpointed | migration read only |
| findings/evidence | verified observations | TaskState evidence | checkpointed | migration read only |
| status/phase/blocker | lifecycle and blockers | TaskState status/blockers | checkpointed | migration read only |
| archived_findings | old observations | migration evidence | can be retained in checkpoint | no active subsystem |
| observed_calls | runtime observations | transcript/trace | rebuildable | no active subsystem |

The stale `WORKING_MEMORY_*` entries were removed from `.env.example`; no production reader exists.

## Session Metadata Classification

| Category | Keys observed |
|---|---|
| routing/identity | `user_id`, `user_role`, `last_route`, `model_profile`, `thinking_enabled` |
| runtime/trace | `run_id`, `parent_run_id`, `context_metrics`, `memory_trace_events` |
| task | `kind`, `task_id`, `status`, `task_reply`, `subagent_runner_available` |
| legacy state migration | `task_state`, `working_memory`, `coding_context_state` |
| workspace | `workspace_root`, `workspace_id`, `project_id`, `repository`, `code_revision` |
| tool | `unlocked_tools`, tool-result/artifact references |
| memory | `memory_root` plus semantic owner inputs derived from identity/workspace |
| UI | `title`, `display_content`, `attachments` |
| compatibility | legacy state payloads and old coding application path/status fields |

Typed state duplicates in metadata are read-only migration sources, not concurrent writers. Removing them requires a persisted-data migration and is P2.

## Prompt Ownership

- Global system is assembled by `ContextBuilder`, but currently concatenates AgentSpec, repository instructions, the full skill catalog and generic runtime guidance.
- AgentSpec owns chat/coding behavior.
- Structured providers own history, TaskState, memory, retrieval and coding context.
- Tool schemas own tool semantics; the runtime-generated tool catalog repeats visible names/descriptions.
- Skills are catalogued eagerly in the prefix and loaded on demand for content.

Safe P0 reductions are duplicate tool catalog prose and duplicated tool-specific runtime guidance. Fundamental profile/skill strategy changes remain P2.
