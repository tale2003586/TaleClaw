# TaleClaw Memory Architecture

## 1. Purpose and boundaries

TaleClaw separates five kinds of state:

| State | Truth source | Sharing rule |
|---|---|---|
| Session History | PostgreSQL Session Store | Current Session transcript |
| Episodic History | Session Store; Qdrant is a derived event index | Normal chat: current user + current Session; Coding: trusted Task/Workspace/Project |
| Long-term Semantic Memory | PostgreSQL `memory_items` and `memory_evidence` | Active, current, unexpired items in trusted owner scopes |
| Task State | `applications/coding/task_state.py`; WorkingMemory is a compatibility projection and CodingContextState is renderer metadata | Current work only; no automatic long-term promotion |
| Application-local State | Coding or another vertical Application | Remains local unless converted to a governed proposal |

There is no Minecraft Application in the current repository. The architecture defines an Application boundary but does not ship an empty adapter.

## 2. Dependency direction

```text
memory.domain / memory.commands
          ↑
MemoryCommandService / MemoryPromotionService / retrieval services
          ↑
PostgresMemoryRepository / Qdrant adapters / runtime providers / tools
```

The domain layer imports no PostgreSQL, Qdrant, Markdown, model provider, or Application implementation. Repository protocols expose domain values and classified errors rather than SQL rows or driver types.

## 3. Semantic memory model

`MemoryItem` owns the governed fact and includes:

- stable ID and `OwnerKey(scope, id)`;
- controlled kind: preference, fact, decision, procedure, constraint, relationship;
- controlled status: candidate, active, superseded, revoked, expired, rejected;
- content and deterministic normalized content;
- confidence, salience, validity interval and last confirmation time;
- version, `supersedes_id`, audit timestamps and bounded metadata.

`MemoryEvidence` links an item to source type/ref and optional Session, Task, Workspace and Project IDs. Excerpts are length-limited; full transcripts and artifacts remain in their own truth stores.

Only `active` items whose validity interval includes the current time are retrievable. Terminal statuses do not transition back to active.

## 4. Trusted scopes

`MemoryContext.from_session()` constructs caller scope from server-owned Session metadata. The model cannot choose `owner_id`.

- Normal chat always has user + session.
- Project, Workspace, Application and Task owners are available only when their trusted IDs exist in Session metadata.
- A project-scoped memory cannot be read, updated or revoked from another project.
- Coding proposals prefer Project, then Workspace, then Task. They never silently fall back to user-global scope.

## 5. Write and lifecycle flow

```text
explicit user / inferred candidate / Coding Conclusion
        ↓
MemoryWriteProposal + MemoryContext + Evidence
        ↓
MemoryCommandService
        ↓
scope check → normalization → exact/semantic duplicate → conflict policy
        ↓
PostgreSQL transaction: item/evidence/state + index outbox
        ↓ commit
MemoryIndexSynchronizer → Qdrant
```

Explicit user requests may create active memory. Inferred and Coding inputs call `propose()` and start as candidate. Exact duplicates merge Evidence rather than creating a second current fact. Updates create a new ID/version, atomically mark the previous item superseded, and schedule delete/upsert index events.

Supported commands are remember, propose, confirm, reject, update, revoke and forget. `forget` searches only active items in the caller's trusted scopes and performs logical revoke; it does not physically delete the audit chain.

## 6. Candidate promotion

`MemoryPromotionService` requires independent evidence identities and a confidence threshold. A single inference or repetitions within one Session/Task stay candidate. Negated/corrected candidates require confirmation.

Coding Conclusion Evidence records Task, Workspace, Project, repository revision, evidence file/location and verification. Unverified Coding conclusions require confirmation. `TaskMemoryPromoter` remains a legacy Markdown adapter only when semantic writes are disabled.

## 7. PostgreSQL responsibilities

`PostgresMemoryRepository` owns:

- `memory_items`: content, owner, kind, status, validity and version chain;
- `memory_evidence`: source and bounded evidence references;
- `memory_index_outbox`: retryable upsert/delete operations;
- `memory_schema_versions`: idempotent module schema version.

State changes use transactions, row locks and expected-version checks. `supersede()` updates the previous row, creates the new row, retains Evidence and schedules both index operations atomically.

Schema creation follows the repository's existing idempotent initialization convention. Downgrade means disabling composition; it does not drop tables.

## 8. Qdrant and outbox consistency

Qdrant is never a truth source.

- Semantic collection default: `taleclaw_semantic_memory_v1`.
- One point represents one Memory ID/version.
- Payload contains ID/version/owner/kind/status/validity/digest/index time, not Evidence or transcripts.
- Database commit does not wait for Qdrant.
- Synchronizer failures record retry status, attempt count, next attempt and bounded error.
- A stale upsert event cannot restore revoked memory: synchronizer re-reads PostgreSQL before upsert.
- Semantic search hits are batch-loaded from PostgreSQL and rejected if missing, stale, inactive, expired or out of scope.

The rebuild service enumerates the current active PostgreSQL snapshot and can target one owner. CLI defaults to dry-run and should write to a new empty collection before alias cutover.

## 9. Semantic retrieval and Context

`SemanticMemoryRetrievalService` performs:

```text
query + trusted owners
→ Qdrant candidates
→ PostgreSQL get_many
→ scope/status/validity/version filters
→ relevance + confidence + salience + freshness ranking
→ ID deduplication and top-k
→ <semantic_memory>
```

If Qdrant fails, the service falls back only to PostgreSQL active items in the same trusted owners. Degradation never broadens scope or accepts terminal states. Semantic Memory has an independent Context budget.

## 10. Episodic retrieval and Session isolation

Session turns are indexed one event at a time. New ordinary points use `session:<session_id>` scope and metadata containing user/session/application/workspace/project/task.

`EpisodicBoundary` requires user plus current Session, or a trusted Coding Task/Workspace/Project boundary. Qdrant `search_filtered()` receives these conditions before vector retrieval. Old user-only points lack the required session metadata and therefore cannot enter Context.

Results render as:

```xml
<episodic_history label="past_events">
  ... past_event ...
</episodic_history>
```

Failure degrades to empty results; it never retries with user-only scope. Semantic and Episodic retrieval remain separate from the authoritative Coding TaskState. The current Coding prompt path and its dynamic token budget are documented in `TASK_STATE_CONTEXT_ARCHITECTURE.md`.

## 11. Markdown and legacy adapters

`MemoryStore` is a legacy adapter for migration, compatibility and Coding task-local state. With semantic feature flags enabled:

- normal `memorize` calls `MemoryCommandService` and does not append Markdown;
- normal `recall_memory` calls semantic retrieval and never mixes PENDING/HISTORY;
- non-memory file sections are rejected for normal semantic writes;
- ordinary bootstrap stops HISTORY and RECENT_CONTEXT file updates;
- whole SELF/MEMORY/NOW/PENDING/HISTORY vector writes are disabled;
- Coding task-local history/state can remain during the compatibility window.

`MarkdownMemoryExporter` atomically generates a marked read-only view from PostgreSQL active items. Business decisions never read that export as state.

## 12. Legacy migration

`LegacyMemoryImporter` classifies every known file:

- MEMORY bullets → active legacy facts;
- PENDING.json → candidates;
- PENDING.md → review;
- SELF/NOW → review;
- HISTORY/RECENT_CONTEXT → skip semantic.

It supports dry-run, deterministic idempotency keys, checkpoint resume, itemized reports and repeated execution. It never deletes or rewrites sources. Operational commands and rollback are documented in `docs/migrations/long-term-memory-migration.md`.

## 13. Trace and metrics

Structured events cover candidate/item lifecycle, index synchronization, semantic/episodic retrieval and Context drops. Events use IDs, scopes, kinds, versions, reasons, scores, digests and bounded previews rather than unnecessary full content.

Trace summary reports counts and safe rates for writes, candidates, promotion, rejection, supersede, revoke, duplicate, conflict, retrieval hits, invalid/stale/scope drops, index failures and memory Context token ratio. Zero denominators return `0.0`.

## 14. Feature flags and rollout

| Flag | Effect |
|---|---|
| `SEMANTIC_MEMORY_ENABLED` | Build PostgreSQL semantic services |
| `SEMANTIC_MEMORY_WRITE_ENABLED` | Route normal governed writes to PostgreSQL |
| `SEMANTIC_MEMORY_READ_ENABLED` | Route `recall_memory` to semantic retrieval |
| `SEMANTIC_MEMORY_CONTEXT_ENABLED` | Inject semantic provider output |
| `SEMANTIC_MEMORY_INDEX_ENABLED` | Enable derived Qdrant adapter |
| `MEMORY_LEGACY_HISTORY_FILES_ENABLED` | Temporary ordinary HISTORY/RECENT file compatibility; default off |

Rollout order is schema/services → write → read → Context. Rollback reverses these flags, preserves PostgreSQL facts and outbox events, and never synchronizes legacy Markdown back into PostgreSQL. Permanent removal of legacy files, tables or collections requires separate approval.
