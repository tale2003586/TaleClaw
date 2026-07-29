# TaskState Context Architecture Spec

## Background

TaleClaw's coding prompt currently relies on duplicated mutable state, fixed character section budgets, message-index compaction, and mechanical truncation. Large requests and tool output can be copied into Session metadata, handoff text, WorkingMemory, CodingContextState, and the final prompt.

## Goals

- Preserve an immutable, auditable event history while keeping only a recent active window in prompts.
- Store large bodies once and reference them from events and state.
- Make TaskState the only authoritative mutable task state.
- Combine deterministic evidence extraction with semantic state patches.
- Compact and checkpoint atomically, then release archived events from the active window.
- Assemble prompts against the selected model's real token window and block over-limit calls.

## Functional Requirements

- F1: Every runtime fact has a stable event ID and typed event; checkpoints can be recovered with following events after restart.
- F2: Large user, file, log, API, analysis, test, generated, and tool-result content is stored in a unified artifact store with metadata, ranged reads, search, outline, and sampling.
- F3: TaskState owns objective, constraints, phase, plans, completed work, findings, hypotheses, decisions, actions, questions, blockers, evidence, artifacts, coverage, and execution memory.
- F4: Deterministic extraction creates evidence and coverage; semantic compaction may only propose a structured StatePatch referencing existing evidence.
- F5: Validation and reduction reject invented evidence, illegal transitions, lost constraints, duplicate IDs, and oversized state.
- F6: A successful compaction persists a checkpoint and completion event before advancing the event boundary; failures leave state and boundary unchanged.
- F7: Recent context is selected in complete conversation/tool transaction groups by a dynamic token budget.
- F8: The latest real user request occurs once, outside historical handoff, and can supersede stale state.
- F9: Legacy sessions, WorkingMemory, and CodingContextState migrate idempotently without destroying legacy records.
- F10: Structured metrics distinguish raw, artifact, state, candidate prompt, and sent prompt sizes.

## Non-Functional Requirements

- N1: All four new feature flags default to enabled.
- N2: Existing short sessions and public APIs remain compatible during migration.
- N3: Character and byte limits are secondary protection only; token estimates drive prompt decisions.
- N4: Original event records remain available for audit and exact artifact content remains retrievable.
- N5: Writes involved in compaction use one persistence transaction where a SessionStore is available.

## Out Of Scope

- Replacing the repository's long-term semantic memory/RAG subsystem.
- Deleting historical Session rows or artifacts automatically.
- Guaranteeing semantic extraction when no compactor model is configured; deterministic extraction remains available and semantic failure is explicit.

## Acceptance Criteria

- AC1: A 500,000-character request is externalized and its body is absent from TaskState, metadata, handoff, and prompt.
- AC2: Current request text appears once in a coding task prompt.
- AC3: Tool calls/results remain atomic and unclosed transactions are never archived.
- AC4: Findings require a valid EvidenceRef; unsupported claims become hypotheses.
- AC5: Repeated compactions advance generation and stable event boundaries exactly once.
- AC6: Restart recovery loads the latest valid checkpoint plus later events and can read artifact references.
- AC7: Different model windows produce different dynamic limits; a final hard guard never invokes an over-limit model call.
- AC8: Existing relevant tests and the new unit, integration, migration, and regression tests pass.

