# Cross-Cutting System Audit: Improvement Work Plan

**Date**: 2026-06-16
**Findings doc**: [2026-06-16-system-audit-findings.md](2026-06-16-system-audit-findings.md)

## Phase Summary

| Phase | Scope | Est. effort | Priority |
|-------|-------|-------------|----------|
| P0: Memory Retrieval Fix | Wire recall through vector index, enable candidate pipeline | 1 day | P0 |
| P1: Config Bootstrap | Move side effects from config.py to bootstrap.py | 0.5 day | P0 |
| P2: Session Write Optimization | Incremental save, LRU eviction, WAL mode | 1 day | P0 |
| P3: Tool Safety Hardening | Loop guard improvements, shell safety, task visibility | 0.5 day | P1 |
| P4: Memory Quality | Embedding signal, semantic dedup, candidate store perf | 1 day | P1 |
| P5: Error Handling Standardization | Structured errors, logging module, teammate error propagation | 1 day | P1 |
| P6: Routing & Workspace | Symlink check, intent classifier improvements | 0.5 day | P1 |
| P7: Web Hardening | CGI deprecation, production guidance | 0.5 day | P2 |
| P8: Misc P2 Items | Plugin setattr, session expiry, stale SYSTEM prompt, dead code | 0.5 day | P2 |
| P9: Test Gap Coverage | ModelPool fallback, Session concurrency, Memory E2E | 1.5 days | P1 |

**Total estimated**: ~7 days

---

## Dependency Map

```
P0 (Memory Retrieval) ──→ P4 (Memory Quality)
P1 (Config Bootstrap) ── independent
P2 (Session Write)    ── independent
P3 (Tool Safety)      ── independent
P5 (Error Handling)   ── independent
P6 (Routing/Workspace)── independent
P7 (Web)              ── independent
P8 (Misc P2)          ── after P0-P7
P9 (Tests)            ── after P0-P6
```

P0 through P7 are independent and can run in parallel.

---

## P0: Memory Retrieval Fix

**Files to modify**:
- [ ] `memory/store.py` — `recall()`: replace full dump with vector search through `history_vector_index`
- [ ] `memory/vector_runtime.py` — default `HISTORY_VECTOR_ENABLED` to `True`; add env `MEMORY_VECTOR_ENABLED` alias
- [ ] `runtime/context.py` — `_build_memory_block()`: use `memory_store.recall(current_request)` instead of `memory_store.read_all()`
- [ ] `memory/lifecycle.py` — `after_turn()`: index memory file content changes into vector store (not just session turns)

**Acceptance criteria**:
1. `memory_store.recall("pytest preferences")` returns relevant memory snippets, not full dump
2. `HISTORY_VECTOR_ENABLED` defaults to `True` — candidate memory pipeline is live
3. Memory block in context is bounded by budget, not by total memory size
4. Existing memory tests pass

---

## P1: Config Bootstrap

**Files to modify**:
- [ ] `config.py` — remove: `load_dotenv(override=True)`, proxy setup, `MODEL_POOL` build, `MODEL_HEALTHCHECK_RESULTS`, `MODEL`, `SYSTEM`
- [ ] `config.py` — keep only: constant definitions, defaults, Path declarations
- [ ] `runtime/bootstrap.py` — absorb: dotenv loading, proxy setup, model pool build, health checks
- [ ] `runtime/bootstrap.py` — add `get_system_prompt()` function (was `SYSTEM` constant)

**Files that import from config (fixup)**:
- [ ] `runtime/agent_loop.py` — if it uses `SYSTEM`, switch to `get_system_prompt()`
- [ ] Any file using `MODEL` or `MODEL_POOL` — should already go through `bootstrap.py`

**Acceptance criteria**:
1. `python -c "import config"` has no side effects (no env mutation, no API calls)
2. `python -c "from runtime.bootstrap import build_runtime"` performs all init
3. `SYSTEM` prompt reflects current skill loader state (not import-time snapshot)
4. All existing tests pass

---

## P2: Session Write Optimization

**Files to modify**:
- [ ] `sessions/session_store.py` — `save_session()`: track `last_saved_seq`, only INSERT new messages
- [ ] `sessions/session.py` — `SessionManager.get_or_create()`: add LRU eviction (max 128 sessions in memory)
- [ ] `runtime/db.py` — `connect()`: add `PRAGMA journal_mode=WAL` for SQLite connections

**Acceptance criteria**:
1. Second `save_session()` call on same session writes 0 message rows (if no new messages)
2. Session cache doesn't exceed 128 entries; oldest idle sessions evicted
3. SQLite connections use WAL mode
4. Existing session tests pass

---

## P3: Tool Safety Hardening

**Files to modify**:
- [ ] `tools/hooks.py` — `ToolLoopGuardHook`: add tool-name-only counter (same tool >5 times in window = deny)
- [ ] `tools/hooks.py` — `ShellSafetyHook`: add more patterns (`rm -rf`, `mkfs`, `dd if=`)
- [ ] `tools/policy.py` — move `task` from `DEFERRED_TOOLS` to `PRELOADED_TOOLS_BY_MODE["coding"]`

**Acceptance criteria**:
1. Model calling `bash` 6 times in a row (different commands) triggers loop guard
2. `rm -rf .` and `mkfs` are blocked
3. Lead agent sees `task` in visible tools without `tool_search select:task`
4. Existing tool tests pass

---

## P4: Memory Quality

**Files to modify**:
- [ ] `memory/lifecycle.py` — `_render_messages_for_embedding()`: extract text fields from structured content, skip JSON blobs
- [ ] `memory/dedup.py` — add `is_semantic_duplicate()` using embedding cosine similarity (threshold 0.92)
- [ ] `memory/candidates.py` — `CandidateMemoryStore`: migrate from JSON file to SQLite table (schema: id, content, type, confidence, evidence_count, source_refs, created_at, updated_at, status, metadata)

**Acceptance criteria**:
1. Embedding text for tool results shows human-readable summary, not raw JSON
2. "Use pytest for testing" and "Use pytest to run tests" are detected as near-duplicates
3. `CandidateMemoryStore.upsert()` performs single-row INSERT/UPDATE, not full-file read/write
4. Migration script converts existing `PENDING.json` to SQLite on first run

---

## P5: Error Handling Standardization

**Files to modify**:
- [ ] `tools/handlers.py` — normalize 18 `except Exception` blocks to return `f"Error: {type(e).__name__}: {e}"` consistently
- [ ] `coding_runtime/teammate.py` — `_run_member` error handler: send `MessageType.ERROR` to lead via BUS
- [ ] `models/model_task_runner.py` — add optional `on_error` callback

**Files to create**:
- [ ] `runtime/logging.py` — `setup_logging()`: stdout + file handler with timestamps and levels

**Acceptance criteria**:
1. All `except Exception` blocks in `tools/handlers.py` use consistent error format
2. Teammate crash is visible to lead (error message in lead inbox)
3. `setup_logging()` provides INFO-level stdout + DEBUG-level file logging
4. Existing tests pass

---

## P6: Routing & Workspace

**Files to modify**:
- [ ] `runtime/workspace.py` — `safe_workspace_path()`: `target.resolve()` before containment check; require `session` parameter
- [ ] `runtime/routing/intent.py` — add embedding-based fallback for ambiguous keyword matches (confidence 0.62 cases)
- [ ] `runtime/routing/execution_plan.py` — remove duplicate mode switch detection (delegate to IntentClassifier)

**Acceptance criteria**:
1. Symlink inside workspace pointing outside is blocked by `safe_workspace_path()`
2. `safe_workspace_path(path)` without session raises `ValueError`
3. Intent classification for ambiguous queries uses embedding similarity as tiebreaker
4. Mode switch logic lives in one place (IntentClassifier)

---

## P7: Web Hardening

**Files to modify**:
- [ ] `web/server.py` — replace `cgi` with `email.parser` + `urllib.parse` for multipart form parsing
- [ ] `web/server.py` — add docstring: production deployment guidance (nginx reverse proxy)

**Acceptance criteria**:
1. No `import cgi` in codebase
2. Multipart file upload works on Python 3.13+
3. Docstring documents production deployment path

---

## P8: Misc P2 Items

**Files to modify**:
- [ ] `plugins/plugin_manager.py` — replace `setattr(plugin, "_plugin_manager", self)` with `_plugin_refs: WeakKeyDictionary`
- [ ] `sessions/session.py` — add `cleanup_expired_sessions()`: delete sessions with `updated_at` older than N days
- [ ] `memory/archive_store.py` — change default DB path to `.sessions/memory_archive.db`
- [ ] `memory/history_summary.py` — remove unused `provider`/`model` constructor parameters
- [ ] `memory/candidates.py` — move `_MEMORY_KEYWORDS` to env-configurable list
- [ ] `runtime/routing/intent.py` — consolidate mode switch detection from ExecutionPlanner

**Acceptance criteria**:
1. Plugin registration works with `__slots__` and frozen dataclass plugins
2. `cleanup_expired_sessions()` removes stale sessions from DB
3. Memory archive uses independent database file
4. HistorySummarizer constructor is simplified

---

## P9: Test Gap Coverage

**Files to create**:
- [ ] `tests/test_model_pool_fallback.py` — provider failover, health marking, cooldown
- [ ] `tests/test_session_concurrency.py` — concurrent reads/writes, LRU eviction
- [ ] `tests/test_memory_lifecycle_e2e.py` — turn → candidate → promotion → recall (with vector index enabled)
- [ ] `tests/test_candidate_store_sqlite.py` — migration, upsert performance, concurrent writes
- [ ] `tests/test_workspace_resolver.py` — symlink escape, non-existent paths, session binding

**Acceptance criteria**:
1. ModelPool fallback: primary fails → secondary used → primary recovers after cooldown
2. Session concurrency: 10 threads reading/writing same session → no corruption
3. Memory E2E: 5 turns about pytest → candidate detected → confidence ≥ 0.85 → promoted → recall returns it
4. All new tests pass

---

## Rollback & Risk

| Risk | Mitigation |
|------|------------|
| Enabling `HISTORY_VECTOR_ENABLED` by default may break if Qdrant is not running | Graceful fallback: if Qdrant connection fails at bootstrap, disable vector features and log warning |
| Config.py refactoring may break imports | Git grep all `from config import` before starting; update all at once |
| Session incremental save may miss messages | Track `last_saved_seq` per session; test with add→save→add→save sequence |
| Candidate store migration from JSON to SQLite | Auto-migrate on first read: if `PENDING.json` exists and SQLite table is empty, import |
| Safe workspace symlink fix may block legitimate workflows | Only check that RESOLVED path is within workspace, not that the path has no symlinks |
