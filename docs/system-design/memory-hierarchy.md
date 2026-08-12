# State And Memory Ownership

| Data | Authoritative owner | Persistence |
|---|---|---|
| Conversation turns | Session | session repository |
| Task objective and progress | TaskState | `session.metadata.task_state` |
| Compaction boundary and summary | ContextSnapshot | session snapshot records |
| Temporary evidence and hypotheses | Session events / TaskState | session repository |
| Durable preferences, facts, conventions, decisions | MemoryRepository | PostgreSQL |
| Semantic lookup vectors | MemoryIndex | derived, rebuildable |

Coding task sessions do not create a `memory/` directory and do not copy global
memory into their prompt. Their structured conclusions are stored with the task
artifacts. When semantic memory writes are enabled, only verified reusable
conclusions cross the boundary through `MemoryCommandService`.

Old WorkingMemory and CodingContextState payloads are accepted only as migration
inputs and are converted to TaskState. New sessions never write either key.
