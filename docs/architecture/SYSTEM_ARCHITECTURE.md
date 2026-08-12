# TaleClaw Current Architecture

This document describes the current runtime after aggressive pruning. Historical
phase reports under `docs/architecture/runtime-phase*`, `docs/workplan`, and
`docs/reports` describe earlier implementations and are not current contracts.

## Execution

```text
CLI / Web / Telegram / Feishu
             |
             v
        AppRuntime
             |
             v
      TurnCoordinator
       |     |      |
       |     |      +--> direct reply
       |     +---------> CodingApplication
       +---------------> Runtime.run
                              |
                              v
                         AgentRunner
                              |
                              v
                        ReasoningLoop
                       /      |       \
              ContextBuilder Model ToolExecutor
```

`applications/bootstrap.py` is the composition root. `Runtime.run(agent,
input, context)` is the sole public execution entry. `AgentRunner` resolves
model policy and constructs execution policies. `ReasoningLoop` only builds
model input, calls the model, executes requested tools, updates per-run state,
and stops or continues.

Coding owns task-session creation, workspace binding, conclusions, artifacts,
and optional durable-memory promotion. TaskState observes generic run decisions
through `RuntimeExtensions`; the core loop does not import or mutate TaskState.
Tool-result artifact creation is owned by `ToolResultStoreHook`, which records
the artifact event between the assistant tool request and tool result.

## Ownership

| Fact or behavior | Owner |
|---|---|
| Conversation history | `Session` / `SessionStore` |
| Agent behavior instructions | `AgentSpec` |
| Application routing | `TurnCoordinator` and routing application |
| Mutable run counters and stop decision | `RunExecutionState` |
| Optional task objective/progress/status | `TaskStateCore` service |
| Compaction boundary and summary | `ContextSnapshot` |
| Prompt assembly | `ContextBuilder` plus explicit contributors |
| Tool visibility | `ToolRegistry` / `ToolPolicy` |
| Tool execution and safety | `ToolExecutor` and hooks |
| Durable cross-session memory | `MemoryRepository` |
| Semantic lookup vectors | derived `MemoryIndex` |
| Trace persistence and aggregation | `TraceStore` |

There is no behavior Profile layer, mode prompt layer, Pipeline execution API,
WorkingMemory runtime, or CodingContextState task-progress owner.

## Context

Static prompt material has four sources:

1. a minimal runtime contract;
2. the selected `AgentSpec` instructions;
3. dynamic structured context from explicit contributors;
4. session history and the current interaction.

Skills are loaded only after a matching tool/capability path selects them. The
catalog is not injected into ordinary turns. Coding context is a read-only view
projected from TaskState, session events, and ContextSnapshot; it does not own a
second copy of progress.

## Tools

Chat starts with only `tool_search`. Coding starts with eight primitives for
inspection, search, editing, shell execution, and capability discovery. Rare,
specialized, administrative, memory, artifact, and subagent tools remain
installed but deferred. `tool_search` filters deterministically by AgentSpec,
mode, role, policy, and current unlock state; it is not another model router.

Explicit `parallel_tasks` and `read_files` tools remain available when selected.
The core loop no longer rewrites repeated `task` or `read_file` calls into hidden
batch calls.

## Optional Features

Memory, RAG, skills, subagents, artifacts, full trace, and background/team
orchestration are composed at application boundaries. With semantic and
episodic memory disabled, importing `applications.bootstrap` loads no
`memory.*` implementation modules.

The durable-memory flow is:

```text
verified fact -> MemoryCommandService -> MemoryRepository
                                      -> outbox -> derived index

request -> SemanticMemoryRetrievalService -> selected context contribution
```

Conversation history, TaskState, temporary evidence, and compaction snapshots
are not long-term memory.

## Persisted Legacy Data

Two read-only migration boundaries remain:

- `runtime/task_state/legacy.py` reads old `working_memory` and
  `coding_context_state` payloads, writes current TaskState plus a migration
  checkpoint, then removes the old keys. Delete it after supported persisted
  sessions have been migrated or expired.
- `memory/migration/` imports legacy Markdown memory into the repository.
  `legacy_files.py` supports the read-only legacy Web view. Delete these after
  user directories are imported and that view is retired.

New sessions never write either legacy task key, and legacy Markdown is never a
new source of truth.
