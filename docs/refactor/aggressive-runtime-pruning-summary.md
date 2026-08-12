# Aggressive Runtime Pruning Summary

Phase 2 baseline: `e7f08ae763e8cdccf5831ec4e08304ea8274fd73`.
Measurements in this report use tracked files only and exclude generated runtime
state. The final benchmark is deterministic and offline.

## Executive Summary

Phase 2 removed alternate runtime architectures rather than wrapping them:

- deleted the default memory lifecycle, candidate, enrichment, governance,
  archive, task-local Markdown, note/relation, and background stacks;
- retired WorkingMemory and CodingContextState as runtime state owners;
- reduced default model-visible tools from 10 to 1 for Chat and from 32 to 8
  for Coding;
- made AgentSpec the only behavior-instruction owner and removed Profile/mode
  prompt layers and eager skill catalogs;
- removed `run_turn`, `_run_turn`, Pipeline aliases, hidden tool batching, and
  application-owned TaskState/artifact behavior from ReasoningLoop;
- made memory implementation imports conditional at the composition root.

Tracked Python fell from 84,554 to 79,583 LOC (-4,971, 5.9%). Memory fell from
5,877 to 3,397 LOC (-42.2%); ReasoningLoop fell from 2,077 to 1,531 LOC
(-26.3%). All retained product capabilities are covered by the current suite.

## Architecture Before / After

Before:

```text
Application -> Profile/Mode/AgentSpec -> Pipeline/run_turn/_run_turn
  -> ReasoningLoop
     + task progress mutation
     + artifact event production
     + hidden tool batching
     + product-specific policy branches

Every bootstrap -> memory handlers/lifecycle implementations
```

After:

```text
Application -> AgentSpec -> Runtime.run -> AgentRunner -> ReasoningLoop
                                                 |       |       |
                                         ContextBuilder Model ToolExecutor

Optional extensions: TaskState observer, durable memory, skills, RAG,
subagents, artifacts, trace, and application lifecycle
```

`AgentRunner` remains because it resolves model policy, constructs the selected
execution policies, binds the context builder and tool view, and creates the
per-run loop. It is not a forwarding alias.

## Product Capability Matrix

The auditable before/current owner/test matrix is maintained in
[`product-capability-matrix.md`](product-capability-matrix.md). Simple chat,
conversation continuity, coding inspect/search/edit/shell, safety, isolation,
cancellation, streaming, compaction, optional TaskState, durable memory,
subagents, skills, and artifact offload all remain. Historical implementation
tests were not treated as product contracts.

## Memory Before / After

Before, every turn could be connected to a 32-module stack containing recent
history summaries, archives, candidates, enrichment, governance, promotion,
background lifecycle, task-local Markdown stores, and multiple vector paths.
Several paths were disabled or produced only traces.

After, durable memory is explicit and optional:

```text
verified write -> MemoryCommandService -> MemoryRepository (truth)
                                      -> outbox -> derived semantic index

request -> SemanticMemoryRetrievalService -> authorized active records
        -> ContextMemoryService -> budgeted context
```

The 20 remaining Python modules are justified as follows:

| Modules | Required responsibility |
|---|---|
| `domain.py`, `commands.py` | Durable item/evidence schema and controlled command DTOs. |
| `repository.py`, `postgres_repository.py` | Repository contract and authoritative PostgreSQL persistence. |
| `command_service.py`, `conflict_service.py`, `dedup.py` | Access checks, transitions, idempotent writes, and conflict handling. |
| `semantic_retrieval.py` | Scoped selection of active durable records for explicit or automatic recall. |
| `semantic_index.py`, `index_sync.py` | Rebuildable lookup contract and repository-outbox reconciliation. |
| `embeddings.py` | Lazy embedding providers shared by semantic memory and optional Security RAG. |
| `qdrant_index.py`, `vector_index.py`, `vector_runtime.py` | Optional derived Qdrant/history index contracts and lazy factories; never truth. |
| `episodic_retrieval.py` | Optional retrieval over Session-owned episodic records; it has no durable-memory writer. |
| `migration/legacy_importer.py`, `migration/legacy_files.py` | One-way legacy import and read-only Web display of unmigrated user files. |
| `migration/rebuild_index.py` | Rebuild a derived index from repository truth. |
| package `__init__.py` files | Package/export boundaries only. |

The unused Markdown exporter and its test were deleted in the final sweep. With
all memory flags off, ordinary Chat starts and completes normally; a fresh
process importing `applications.bootstrap` loads zero `memory.*` modules.

## State Ownership Before / After

| Fact | Before | Current single owner |
|---|---|---|
| Conversation | Session plus memory/history copies | `Session` / `SessionStore` |
| Task progress | WorkingMemory, CodingContextState, TaskState | TaskState core |
| Compaction boundary/summary | CodingContextState mixed with progress | `ContextSnapshot` |
| Active evidence | persisted working-memory fields | session events; rebuildable |
| Per-run counters/stop | loop/session metadata | `RunExecutionState` |
| Durable cross-session fact | Markdown plus PostgreSQL/vector paths | `MemoryRepository` |

New sessions write zero `working_memory`, `coding_context_state`, or
`memory_root` metadata keys. Coding context is a read-only projection from
TaskState, session events, and ContextSnapshot; it does not write progress.

## Tool Surface Before / After

| Surface | Before visible | Current visible | Before tool tokens | Current tool tokens |
|---|---:|---:|---:|---:|
| Chat | 10 | 1 | 1,758 | 122 |
| Coding | 32 | 8 | 7,133 | 2,816 |
| Explore subagent | 15 | 6 | 4,311 | 2,558 |

Chat exposes only `tool_search`, required to discover and explicitly unlock an
optional capability. Memory, skills, storage, sandbox, artifacts, TaskState,
and subagents are deferred.

Coding's eight primitives each have a distinct default role:

| Tool | Why visible at coding-task start |
|---|---|
| `bash` | Run builds, tests, version-control commands, and non-file tooling. |
| `list_files` | Bounded structured discovery without shell quoting. |
| `rg` | Fast content/symbol search before reading. |
| `read_file` | Bounded source inspection. |
| `write_file` | Structured creation or complete replacement under workspace policy. |
| `edit_file` | Focused deterministic edits without reconstructing an entire file. |
| `update_task_state` | Persist progress only when a long task uses TaskState. |
| `tool_search` | Deterministically unlock specialized/rare capabilities. |

Explore receives only `bash`, `list_files`, `rg`, `read_file`,
`update_task_state`, and `tool_search`; it cannot edit. The separate teammate
mode has ten defaults because it additionally performs edits and team
coordination (`send_message`, `idle`). `tool_search` filters registered tools by
mode, role, ToolSpec policy, and current unlock state; it invokes no model.

## Prompt Ownership Before / After

Before, global system text, Profile, AgentSpec, mode instruction files, eager
skill catalog, repository instructions, and tool descriptions overlapped.

Now prompt material has four sources: minimal runtime contract, AgentSpec,
dynamic structured context, and conversation/current input. AgentSpec alone
owns behavior instructions. Model policy contains model/parameter choices;
`tool_mode` selects capabilities but contributes no behavior prose. There is no
Profile instruction or mode prompt layer. Full skill instructions load only
after selection; an unrelated request pays 0 skill-catalog tokens.

## Runtime Core Before / After

ReasoningLoop now builds model input, calls the selected model, executes the
requested tools, updates generic run state, invokes extension observers, and
continues or stops. Removed responsibilities include TaskState imports and
mutation, artifact event construction, subagent-output parsing, automatic
`task -> parallel_tasks` and `read_file -> read_files` rewriting, and agent-mode
checkpoint policy.

The loop contains no application-specific execution branch. It consumes a
generic tool-call policy; the application-selected standard policy may enforce
a Web search budget outside the loop. `coding_context` remains only as a trace
metric key reported by ContextBuilder, not a behavior switch. Checkpointing is
owned by the caller through an explicit callback.

## Compatibility Removed

- `Runtime.run_turn`, `Runtime._run_turn`, Pipeline type/field aliases, and
  `base_pipeline` production naming;
- `AgentSpec.from_profile`, Profile behavior instructions, ModeProfile, and
  mode prompt files;
- WorkingMemory readers/writers/providers/prompts/config and current-session
  CodingContextState persistence;
- default memory lifecycle/candidate/archive/governance/enrichment stack;
- legacy Markdown writes and task-local memory roots;
- eager skill catalog and default memory/storage/artifact tool schemas;
- ReasoningLoop hidden batch rewrites and old subagent trace-derived state.

Production Python has no `run_turn`, `_run_turn`, `base_pipeline`, ModeProfile,
or `from_profile` entry. Remaining `pipeline` text is historical documentation,
benchmark history, or Security RAG terminology.

## Legacy Migration Remaining

| Legacy input | Boundary and behavior | Removal condition |
|---|---|---|
| `working_memory`, `coding_context_state`, pre-v2 flat task payload | `runtime/task_state/legacy.py` reads once, checkpoints TaskState v2, then removes old keys. | All supported sessions migrated/audited or expired past retention. |
| Legacy Markdown/JSON user memory | `memory/migration/` imports into the repository; `legacy_files.py` is read-only for the old Web view. | All user roots imported and the legacy view retired. |
| Session DB `current_mode` column | `SessionStore` copies to `active_agent` and drops the old column transactionally. | All supported session databases have opened on the new schema. |
| Message-only sessions and old tool-result files | Session/result-store adapters backfill immutable events or read old artifacts without emitting the old format. | Stored rows/artifacts migrated or expired under retention. |

These adapters exist only to preserve persisted user data. They do not enter
normal new-session writes, model context as alternate truth, or core execution.

## Deleted Tests / Updated Tests

- 9 test files were deleted with removed features, including candidate store,
  enrichment, governance, lifecycle/archive, notes, Markdown export, promotion,
  and legacy recall implementation suites.
- 52 test files were updated when tool visibility, prompt ownership, Runtime
  entry, state ownership, and observer contracts changed.
- Diff-level test functions: 78 removed, 25 added, net -53. The added tests
  cover memory-off imports, retired-key non-writing, one-way migration,
  constrained default tools, no eager skill catalog, TaskState observers, and
  generic checkpoint ownership.
- Baseline: 132 Python test-support files, 20,901 LOC, 689 passed. Current: 123
  files, 19,469 LOC, 632 passed.

## Token Benchmark

The before values are Phase 1's real offline request capture. Current values
were captured from the first real `Runtime.run` model request with the same
token estimator; tool schemas are compact JSON in a system-text envelope.

| Scenario | System tokens before -> current | Context tokens before -> current | Tools before -> current | Static total before -> current |
|---|---:|---:|---:|---:|
| Simple Chat | 780 -> 249 | 850 -> 272 | 1,758 -> 122 | 2,608 -> 394 (-84.9%) |
| Coding read-only | 3,184 -> 802 | 3,350 -> 832 | 7,133 -> 2,816 | 10,483 -> 3,648 (-65.2%) |
| Explore subagent | 3,654 -> 1,094 | 3,783 -> 1,149 | 4,311 -> 2,558 | 8,094 -> 3,707 (-54.2%) |

For a request that calls no tools, the static tool-schema cost is therefore
122 Chat tokens, 2,816 Coding tokens, or 2,558 Explore tokens. Skill catalog
cost is 0 tokens unless a skill is selected. The benchmark fixture's smaller
synthetic context is also recorded in `runtime_phase0.json` as Chat 186 and
Coding 649 tokens; it is not substituted for the real request capture above.

## LOC Benchmark

| Area | Baseline | Current | Delta |
|---|---:|---:|---:|
| Total tracked Python | 84,554 | 79,583 | -4,971 (-5.9%) |
| Production Python | 63,653 | 60,114 | -3,539 (-5.6%) |
| Runtime | 18,212 | 17,603 | -609 (-3.3%) |
| Memory | 5,877 | 3,397 | -2,480 (-42.2%) |
| Tools | 6,133 | 6,148 | +15 (+0.2%) |
| Applications | 6,160 | 5,764 | -396 (-6.4%) |
| Agents | 2,062 | 2,047 | -15 (-0.7%) |
| Tests | 20,901 | 19,469 | -1,432 (-6.9%) |
| Memory Python modules | 32 | 20 | -12 (-37.5%) |
| ReasoningLoop | 2,077 | 1,531 | -546 (-26.3%) |

The small Tools increase is the explicit policy/hook ownership needed to
remove artifact and capability behavior from Runtime; it did not increase the
model-visible surface.

Legacy-state metrics are both zero for new sessions: zero writers for retired
WorkingMemory/CodingContextState fields and zero retired metadata keys emitted.
The two key names remain solely as migration input constants.

## Performance Benchmark

`python benchmarks/runtime_phase0.py --iterations 20` is offline and excludes
network/model latency. Baseline ran on Python 3.14/macOS arm64; current ran on
Python 3.12/Linux x86_64, so absolute before/after timings are indicative and
must not be presented as same-host speedups.

| Scenario | Baseline median / p95 ms | Current median / p95 ms |
|---|---:|---:|
| Runtime construction | 0.037 / 0.042 | 0.057 / 0.060 |
| Chat no tool | 0.692 / 0.832 | 0.498 / 0.613 |
| Chat one tool | 1.346 / 1.487 | 0.959 / 1.014 |
| Chat three tools | 2.835 / 3.257 | 1.051 / 1.267 |
| Chat context build | 0.201 / 0.311 | 0.203 / 0.329 |
| Coding context build | 0.279 / 1.014 | 1.072 / 1.157 |
| Disk trace write | 4.918 / 5.705 | 4.026 / 4.175 |
| Streaming | 0.692 / 0.785 | 0.493 / 0.521 |
| Cancellation before model | 0.185 / 0.195 | 0.212 / 0.355 |
| Subagent create/return | 2.485 / 2.598 | 2.545 / 2.657 |

## Verification

- `python -m pytest --collect-only -q`: 632 tests collected.
- The full collected Python set passed: 631 tests across four file partitions,
  plus the isolated 29-second coding benchmark test, for 632 passed total. The
  single-process invocation was also attempted but its output session ended
  before pytest returned a summary, so the report relies on the complete
  partitioned run rather than claiming an unobserved exit status.
- `cd web/frontend && npm test -- --run`: 26 passed.
- Memory/retrieval closure: 16 passed; runtime policy/state/trace closure: 42
  passed; memory-off bootstrap and retired-key checks: 3 passed.
- `git diff --check`: passed.

## Acceptance Answers

1. Every major `memory/` group is justified in the Memory table; no retained
   module exists only because it has tests.
2. Yes. Memory-off Chat starts and runs; the tested bootstrap imports no memory
   implementation.
3. Yes. WorkingMemory has no normal reader, writer, provider, prompt, tool, or
   Runtime dependency.
4. No. New sessions emit none of the three retired metadata keys.
5. No. Legacy Markdown is read-only migration/display input; PostgreSQL
   `MemoryRepository` is durable truth.
6. No. TaskState owns progress; coding context projects it and owns only
   rendering/compaction boundaries.
7. Chat needs only `tool_search`, to activate an explicitly requested optional
   capability.
8. Coding's eight tools and their distinct primitive roles are listed above.
9. A no-tool call still sends schemas costing 122 Chat, 2,816 Coding, or 2,558
   Explore tokens.
10. AgentSpec owns behavior. Model/Profile data owns model parameters; mode is
    capability selection only and contributes no instruction layer.
11. An unrelated request pays 0 skill-catalog tokens.
12. ReasoningLoop has no application-specific execution branch. Concrete Web
    search budgeting is injected behind a generic policy, and coding context is
    only a trace metric name.
13. Memory-off Core imports zero `memory.*` implementations in a fresh process.
14. Yes. `run_turn`, `_run_turn`, Pipeline, and `base_pipeline` have exited
    production Python.
15. Remaining compatibility is limited to persisted task/session/message,
    tool-result, and Markdown inputs; every adapter and deletion condition is
    listed in Legacy Migration Remaining.

## Remaining P1

None in the Phase 2 runtime-pruning scope. Operational rollout should run the
documented migration commands and observe migration counts; that is deployment
work, not a second runtime path.

## Remaining P2

Set and execute retention deadlines for legacy sessions, Markdown user roots,
and old tool-result artifacts. Their adapters cannot be deleted before the data
is migrated or expires, because doing so would irreversibly orphan user data.
