# TaskState Context Architecture Checklist

## Storage And Input

- [x] A 500,000-character user message is stored once as an artifact and absent from the model prompt (verify: long-content integration test).
- [x] Artifact metadata, exact/ranged read, search, outline, and sample work after restart (verify: ArtifactStore unit tests).
- [x] New facts receive stable event IDs and legacy sessions migrate without deleted history (verify: SessionStore migration tests).

## State And Compaction

- [x] TaskState is the only mutable task authority; compatibility context is rendered from it (verify: state persistence tests and metadata assertions).
- [x] Findings only accept registered evidence and unsupported semantic claims become hypotheses (verify: validator tests).
- [x] Action and phase transitions reject illegal changes (verify: reducer transition tests).
- [x] Multi-generation compaction advances each event once and never archives an open tool transaction (verify: compaction integration tests).
- [x] Semantic, validation, artifact, checkpoint, and database failures do not advance the boundary (verify: injected-failure tests).

## Prompt And Budget

- [x] Handoff excludes the current request and the task prompt owns the active request (verify: coding runner regression test).
- [x] Recent raw tail uses complete token-budgeted groups and retains the latest real user message (verify: prompt assembly tests).
- [x] Model window, system/tools, output reserve, and margin affect the calculated budget (verify: dynamic budget tests).
- [x] An over-limit prompt raises before provider invocation and increments a hard-block metric, including when dynamic assembly is disabled (verify: reasoning loop tests with spy provider).

## Recovery And Compatibility

- [x] Restart restores the latest checksum-valid checkpoint plus later active events (verify: in-memory restart tests; PostgreSQL restart test is present and skips when the service is unavailable).
- [x] WorkingMemory migration preserves useful completed/pending/evidence fields without full objective duplication (verify: migration unit test).
- [x] CodingContextState migration downgrades unsupported findings and converts do-not-repeat history (verify: migration unit test).
- [x] Existing context, coding, memory, runtime, and Session tests pass (verify: focused suite and repository-wide non-PostgreSQL pytest).

## End To End

- [x] Long coding request -> artifact ref -> event -> TaskState -> provider-safe prompt completes; compaction/checkpoint/retrieval are covered by adjacent integration tests (verify: end-to-end and multi-generation tests).

## Verification Record

- 2026-07-29 focused context/migration/trace suite: `86 passed, 2 skipped, 2 deselected`.
- 2026-07-29 repository-wide non-PostgreSQL suite: `526 passed`.
- PostgreSQL restart tests skip locally because `127.0.0.1:55432` is unavailable; they retain checksum and reload assertions for deployment CI.

## Documentation Evidence

- [x] Canonical architecture defines the data flow, ownership boundaries, state maintenance rules, compaction transaction, recovery, dynamic budget formula, deprecated path, metrics, and known limits (evidence: `docs/architecture/TASK_STATE_CONTEXT_ARCHITECTURE.md`).
- [x] Architecture includes Mermaid data-flow, state-transition, and state-patch/compaction diagrams (evidence: `docs/architecture/TASK_STATE_CONTEXT_ARCHITECTURE.md`).
- [x] Additive legacy Session/WorkingMemory/CodingContextState/large-content migration, failure recovery, rollback, test commands, and manual acceptance are documented (evidence: `docs/migrations/TASK_STATE_CONTEXT_MIGRATION.md`).
- [x] Docs index and unified architecture point to the canonical design; previous fixed-character/dual-state Coding documents are explicitly marked historical (evidence: `docs/README.md`, `docs/architecture/SYSTEM_ARCHITECTURE.md`, `docs/system-design/05-上下文构建、压缩与记忆生命周期.md`, `docs/system-design/11-会话类型、Profile与工作记忆边界.md`).
