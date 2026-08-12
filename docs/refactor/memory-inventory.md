# Memory Inventory

Audit basis: tracked production imports/call sites, test references, bootstrap flags and persistence behavior at `03a1580`.

Classification: M0 core, M1 optional, M2 legacy, M3 experimental, M4 dead.

| File (LOC) | Main symbols / persistence | Runtime path and tests | Default/model/context | Class |
|---|---|---|---|---|
| `domain.py` (218) | owner/kind/status/source enums, item/evidence | semantic repository/commands; broad tests | optional; model indirect | M1 |
| `commands.py` (127) | access context, write proposal, transition | command service/tools; tested | optional | M1 |
| `repository.py` (108) | authoritative repository protocol | semantic services; fakes/tests | optional | M1 |
| `postgres_repository.py` (626) | items/evidence/outbox tables | bootstrap when semantic flags enabled; integration tests | disabled by default | M1 |
| `command_service.py` (280) | dedup/conflict/write/transition | memorize and coding promotion; tested outcomes | disabled by default | M1 |
| `conflict_service.py` (88) | conflict action selection | command service; tested | optional | M1 |
| `dedup.py` (113) | normalized duplicate detection | command service; tested | optional | M1 |
| `promotion_service.py` (88) | candidate promotion policy | bootstrap/coding lifecycle; tested | disabled by default | M1 |
| `semantic_retrieval.py` (240) | scoped ranked retrieval | context memory service + recall; tested | semantic context/read flags | M1 |
| `index_sync.py` (88) | repository outbox -> index | semantic write configuration; tested | disabled by default | M1 |
| `semantic_index.py` (214) | semantic index protocol/runtime | vector builders/scripts/tests | disabled by default | M1 |
| `qdrant_index.py` (166) | Qdrant semantic index | vector runtime; tests | disabled by default, heavy | M1 |
| `embeddings.py` (407) | embedding adapters | vector runtime/RAG | disabled by default, heavy | M1 |
| `markdown_exporter.py` (54) | repository export | migration/admin script tests | never context owner | M1 |
| `migration/legacy_importer.py` (266) | Markdown/PENDING -> repository | explicit migration script; tested | manual only | M2 |
| `migration/rebuild_index.py` (60) | rebuild derived index | explicit script; tested | manual only | M1 |
| `store.py` (321) | `SELF/MEMORY/NOW/PENDING`, history/recent files | legacy context, lifecycle, task local; many compatibility tests | context fallback when semantic retriever absent | M2 |
| `scoped_store.py` (34) | per-user/task legacy store resolver | bootstrap/tools/coding | constructed by default | M2 |
| `history_summary.py` (75) | assistant-turn summarization | lifecycle | lifecycle default at HEAD | M1 |
| `archive_store.py` (144) | evicted recent-turn archive table | lifecycle/bootstrap; integration tests | constructed at HEAD | M1 |
| `lifecycle.py` (632) | post-turn history, vector, candidate, archive | `Runtime._after_turn`; broad tests | enabled at HEAD despite optional features | M1, default pollution |
| `background_lifecycle.py` (98) | thread wrapper around lifecycle | bootstrap/runtime; tested | enabled by default at HEAD | M1, default pollution |
| `processor.py` (253) | similarity candidate extraction | lifecycle | constructed at HEAD; effective only with index | M3 |
| `candidates.py` (270) | JSON pending candidate store/model | processor/lifecycle tests | optional legacy candidate path | M3 |
| `governance.py` (175) | candidate classification/policy | processor when flag enabled; tested | disabled by default | M3 |
| `enrichment.py` (170) | model-assisted pending enrichment | processor when flag enabled; tested | disabled by default | M3 |
| `notes.py` (261) | alternate note model and relation links | governance/enrichment use note origin; link types only tests | disabled; no link consumer | M3; relation subset M4 |
| `evolution.py` (219) | relation/evolution proposal generator | no production caller; unit tests only | never enabled/injected/persisted | M4 |
| `episodic_retrieval.py` (171) | scoped history-boundary retrieval | retrieval services/tests | optional history vector | M1 |
| `vector_index.py` (74) | history vector record/hit protocol | lifecycle/retrieval/tests | derived index, never truth | M1 |
| `vector_runtime.py` (101) | environment factories/scope | bootstrap/scripts | RAG/history flag | M1 |
| `background_lifecycle.py` (98) | asynchronous lifecycle adapter | runtime after-turn | default true at HEAD | M1, default pollution |
| `__init__.py` / migration init (12) | package markers | imports | none | M0 |

## Actual Paths

Semantic write:

```text
memorize/coding conclusion -> MemoryWriteProposal
-> MemoryCommandService (access + dedup + conflict)
-> LongTermMemoryRepository (authoritative Postgres item/evidence/outbox)
-> MemoryIndexSynchronizer -> vector index (derived)
```

Context read:

```text
MemoryContextProvider -> ContextMemoryService
-> semantic retriever when enabled, otherwise legacy scoped file adapter
-> one budgeted memory section -> model context
```

Explicit deeper read:

```text
recall_memory -> semantic retriever when enabled
or legacy scoped store compatibility search
```

Automatic retrieval is a small likely-relevant push; `recall_memory` is intended as explicit deeper pull. At HEAD both are advertised unconditionally, so capability gating is incomplete.

## Relation/Evolution Finding

`memory/evolution.py` produces proposals but has no runtime generator call, persistence, retrieval effect, update effect, or consumer. `MemoryLink` types likewise have no production caller. These are M4 and meet direct-deletion criteria. The trace summary's historical event label is data compatibility, not a runtime producer, and can remain able to render old traces.

## Durable Truth

The intended sole durable semantic truth is `LongTermMemoryRepository`; Qdrant is an index. Legacy Markdown remains a read/migration and task-local compatibility path. Deleting that adapter now could orphan user data, so schema/data convergence is P2 and no user data is touched in this change.
