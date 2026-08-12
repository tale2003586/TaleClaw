# Memory Architecture

TaleClaw has one optional durable-memory system. Conversation history belongs
to Session, task progress belongs to TaskState, and compaction state belongs to
ContextSnapshot. None of those are long-term memory.

## Runtime Flow

```text
explicit user request / verified coding conclusion
    -> MemoryCommandService
    -> MemoryRepository (authoritative PostgreSQL data)
    -> outbox
    -> MemoryIndexSynchronizer
    -> semantic index (derived and rebuildable)

current request
    -> SemanticMemoryRetrievalService
    -> repository authorization and status checks
    -> ContextMemoryService
    -> selected <semantic_memory> context
```

The feature is optional. With all `SEMANTIC_MEMORY_*` flags off, bootstrap does
not construct a repository, index, command service, or retriever. Chat and
Coding continue without a memory context block.

## Ownership

| Concern | Owner |
|---|---|
| Durable item schema and evidence | `memory/domain.py` |
| Authoritative persistence | `memory/repository.py`, `memory/postgres_repository.py` |
| Controlled writes and transitions | `memory/command_service.py` |
| Retrieval and context selection | `memory/semantic_retrieval.py` |
| Derived lookup index | `memory/semantic_index.py`, `memory/qdrant_index.py` |
| Index reconciliation | `memory/index_sync.py` |
| Legacy import | `memory/migration/` |

There is no per-turn candidate/governance/enrichment/archive lifecycle and no
task-local Markdown memory. Verified Coding conclusions may be written directly
through `MemoryCommandService`; unverified conclusions remain task artifacts.

## Legacy Data

`memory/migration/legacy_importer.py` reads old `MEMORY.md`, `PENDING.*`,
`SELF.md`, `NOW.md`, `HISTORY.md`, and `RECENT_CONTEXT.*` files. It does not make
those files a current source of truth. `legacy_files.py` is a read-only adapter
for the Web legacy-memory view and can be removed after all user directories are
imported and that view is retired.

No normal Runtime path writes legacy Markdown or JSON memory files.
