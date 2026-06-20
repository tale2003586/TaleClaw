# Context Management Improvement Work Plan

**Date**: 2026-06-15
**Design doc**: [2026-06-15-context-management-design.md](2026-06-15-context-management-design.md)

## Phase Summary

| Phase | Scope | Est. effort | Priority |
|-------|-------|-------------|----------|
| P0: Unified Compaction | Remove dual compaction, absorb into budget layer | 1.5 days | P0 — correctness |
| P1: Budget Activation | Enable budget by default, re-tune values | 0.5 day | P0 — correctness |
| P2: Pre-Call Token Guard | Token estimator + emergency trim | 0.5 day | P0 — safety |
| P3: Reflection Fix | Rate-limiting + actionable revise | 0.5 day | P0 — cost |
| P4: Context Frame Restructure | Priority ordering, NL rendering, score tiers | 1 day | P1 — quality |
| P5: History Summary Semantics | Tool-chain grouping, configurable keep_recent | 1 day | P1 — quality |
| P6: Misc Improvements | Instruction cache, guidance registry, _build_report signature | 1 day | P2 — maintenance |
| P7: Testing | Unit + integration tests for the full compaction→context pipeline | 1 day | P1 — verification |

## Implementation Status

**Status**: Completed in code on 2026-06-15.

Completed scope:

- P0: Removed `runtime/compact.py`; removed Pipeline/ReasoningLoop dependency on `micro_compact` and `auto_compact`; active-turn tool-result compression now lives in `runtime/context_history.py`.
- P1: Section budget defaults are enabled and retuned; `.env.example` and local `.env` document the new defaults.
- P2: Added `runtime/token_estimator.py`; ReasoningLoop now emits `context_emergency_trim` before model calls when estimated tokens exceed the safe limit.
- P3: Reflection now preserves immediate error triggers and rate-limits normal checks by `REFLECTION_INTERVAL`; revise instructions use `critical="true"`.
- P4: Context frame order is `security_knowledge → task_runtime_events → retrieved_history → memory`; sections include visible priority comments; task runtime events render as natural language; security hits include `[HIGH]/[MEDIUM]/[LOW]` labels.
- P5: Conversation summaries preserve user/assistant/tool-chain semantics; old active-turn tool results are compressed by configurable `keep_recent_results` and `preserve_tools`.
- P6: Added `runtime/context_build_state.py`; `_build_report()` now takes a single `BuildState`; instruction reads use mtime cache; runtime guidance is registry-backed.
- P7: Added focused tests for context budget, token gate, instruction cache, reflection rate limits, and reasoning-loop emergency trim.

Verification:

- `python -m py_compile` passed for changed runtime modules.
- `/home/tale/anaconda3/bin/python -m pytest -q` passed: `212 passed`.
- Root-level pytest collection now uses `pytest.ini` so local data volumes such as `postgres_data` are not collected.

---

## Dependency Map

```
P0 (Unified Compaction)
├──→ P1 (Budget Activation) ──→ P4 (Context Frame) ──→ P7 (Testing)
├──→ P2 (Token Guard) ────────────────────────────────→ P7
└──→ P3 (Reflection Fix) ──────────────────────────────→ P7
                                                             │
P5 (History Semantics) ───────────────────────────────────────┤
P6 (Misc) ────────────────────────────────────────────────────┘
```

P0 is the foundation — must be done first.
P1, P2, P3 can proceed in parallel after P0.
P4, P5, P6 are independent of each other.
P7 gates on all previous phases.

---

## P0: Unified Compaction Pipeline

**Goal**: Remove the dual compaction system (`micro_compact` in Pipeline, `auto_compact` on threshold, budgeter layer), consolidate into budgeter as single source of truth.

**Files to remove**:
- [ ] `runtime/compact.py` — entire file

**Files to modify**:
- [ ] `runtime/context_history.py` — absorb micro_compact logic into `_reduce_active_turn`:
  - Add tool_result culling: after extracting `latest_tool_call_index`, cull old tool results (keep `keep_recent_results` most recent) within the summarized middle, replacing their content with `"<{tool_name} result compressed for context budget>"`
  - Preserve results for tools in `preserve_tools` list
- [ ] `runtime/context_budget.py` — add fields to `SectionBudgetRule`:
  ```python
  keep_recent_results: int = 5
  preserve_tools: tuple[str, ...] = ("read_file", "git_diff", "git_status", "git_log")
  ```
- [ ] `runtime/pipeline.py` — remove from `_before_turn()`:
  ```python
  micro_compact(session.messages)          # ← DELETE
  if estimate_tokens(session.messages) > THRESHOLD:  # ← DELETE block
      print("auto Compacting...")
      session.messages[:] = auto_compact(session.messages)
      session.mark_compacted()
  ```
  Remove from `_before_reasoning()`:
  ```python
  micro_compact(session.messages)          # ← DELETE
  if estimate_tokens(session.messages) > THRESHOLD:  # ← DELETE block
      ...
  ```
- [ ] `config.py` — remove: `KEEP_RECENT`, `PRESERVE_RESULT_TOOLS`, `TRANSCRIPT_DIR`, `THRESHOLD`
- [ ] `runtime/bootstrap.py` — remove any compact-related imports

**Files to modify (import fixups)**:
- [ ] `memory/history_summary.py` — if imports from compact, update to context_history
- [ ] `tests/test_pipeline_tool_loop_guard.py` — if references micro_compact, update
- [ ] Any other file importing from `runtime.compact`

**Acceptance criteria**:
1. `from runtime.compact import micro_compact` fails (file removed)
2. `ContextBuilder.build()` returns messages where old tool results (> `keep_recent_results` ago) are compressed via budget layer, not via Pipeline mutation
3. No `session.messages` mutation occurs during context build (session is read-only until ReasoningLoop appends)
4. All existing tests pass
5. Manual test: 15-step coding task — old tool results are culled from context but recent results are intact

---

## P1: Budget System Activation

**Files to modify**:
- [ ] `runtime/context_budget.py` — `from_env()` default `enabled=True`
- [ ] `runtime/context_budget.py` — re-tune defaults:
  - `memory`: 2500 → 2000
  - `retrieved_history`: 3000 → 2500
  - `security_knowledge`: 4000 → 3000
  - `task_runtime_events`: 2000 → 1500
  - `conversation_history`: 12000 → 10000
- [ ] `.env.example` — document all `CONTEXT_*` env vars with new defaults

**Acceptance criteria**:
1. `ContextBuilder.build()` applies section budgets by default (no env var needed)
2. `CONTEXT_ENABLE_SECTION_BUDGET=false` still disables it (opt-out)
3. Budget report shows sections within their limits for typical conversations
4. Manual test: long conversation (~30 turns) — rendered messages stay within budget

---

## P2: Pre-Call Token Guard

**Files to create**:
- [ ] `runtime/token_estimator.py` — `estimate_tokens()`, `_emergency_trim()`

**Files to modify**:
- [ ] `runtime/reasoning_loop.py` — in `_reasoning_step()`, before calling `method(...)`:
  ```python
  estimated = estimate_tokens(context_messages, provider=provider)
  context_limit = getattr(provider, "context_limit", 128000)
  if estimated > int(context_limit * 0.85):
      context_messages = _emergency_trim(context_messages, int(context_limit * 0.85))
      # trace the trim event
  ```

**Acceptance criteria**:
1. `estimate_tokens()` returns count within ±30% of actual token count for representative messages
2. Emergency trim preserves system message + first 2 turns + last 3 turns
3. Trim event is traced and logged
4. Normal conversations (within limit) are untouched
5. Provider without `context_limit` attribute defaults to 128000 safely

---

## P3: Reflection Optimization

**Files to modify**:
- [ ] `runtime/reflection.py` — `should_reflect()`:
  - Keep immediate triggers (loop_guard, unavailable_tools, tool errors) — always check
  - Periodic check only: `reasoning_steps % reflection_interval == 0`
  - Remove unconditional `reasoning_steps >= min_reasoning_steps` return True
- [ ] `runtime/reflection.py` — `reflect()` when `action="revise"`: add `critical="true"` attribute to instruction tag
- [ ] `config.py` — `REFLECTION_MIN_REASONING_STEPS` default 6 → 10
- [ ] `config.py` — add `REFLECTION_INTERVAL` default 5

**Acceptance criteria**:
1. After step 6 with no errors: reflection checks at step 10, 15, 20 (every 5), not every step
2. Loop guard / tool error / unavailable tool always triggers reflection check immediately
3. Total reflection calls for 20-step task: ≤5 (down from ~14)
4. "revise" instruction includes `critical="true"` in XML tag

---

## P4: Context Frame Restructuring

**Files to modify**:
- [ ] `runtime/context.py` — `_build_context_frame()`:
  - Add HTML comment priority guidance before each section
  - Order: security_knowledge → task_runtime_events → retrieved_history → memory
- [ ] `runtime/context.py` — `_build_task_runtime_events_block()`:
  - Render inbox messages as "[inbox] {sender} ({type}): {content}" not raw JSON
  - Render background results as "[bg:{id}] {status}: {result}"
- [ ] `runtime/context.py` — `_build_security_knowledge_block()`:
  - Add score tier: `[HIGH]` ≥ 0.80, `[MEDIUM]` ≥ 0.60, `[LOW]` < 0.60
- [ ] `runtime/pipeline.py` — `_before_reasoning()`:
  - Move `SECURITY_RAG_AUTO_CONTEXT_USED_KEY = True` to AFTER successful context build
  - Check if security section was actually rendered before marking used

**Acceptance criteria**:
1. Context frame sections have priority comments visible in rendered messages
2. Inbox/background rendering uses natural language, token count reduced by ~30% vs JSON
3. Security hits show score tier labels
4. Router exception doesn't permanently disable security RAG for the turn (flag only set on success)

---

## P5: History Summary Semantics

**Files to modify**:
- [ ] `runtime/context_history.py` — `_summary_message()`:
  - Add `_group_tool_chains()` helper: group [assistant_msg, tool_result1, tool_result2] together
  - New summary format preserves tool-call→tool-result pairing
- [ ] `runtime/context_budget.py` — `SectionBudgetRule`:
  - `keep_recent_results`: 3 → 5
  - `preserve_tools`: add `"git_diff"`, `"git_status"`, `"git_log"` to existing `"read_file"`

**Acceptance criteria**:
1. Compressed history summary shows tool chains as "called tools: X → [tool_id] result: ..."
2. Old tool results up to `keep_recent_results=5` are preserved intact
3. `git_diff`, `git_status`, `git_log` results are never replaced with placeholder
4. Manual test: simulate 30-turn conversation → compact → verify tool chains are understandable in summary

---

## P6: Miscellaneous Improvements

### P6a: _build_report Signature

**Files to create**:
- [ ] `runtime/context_build_state.py` — `_BuildState` dataclass

**Files to modify**:
- [ ] `runtime/context.py` — `build()` populates `_BuildState`; `_build_report(state: _BuildState)` single-param

### P6b: Instruction Caching

**Files to modify**:
- [ ] `runtime/context.py` — `_read_instruction_file()`: add `_instruction_cache` dict with mtime-based invalidation

### P6c: Plugin Guidance Registry

**Files to modify**:
- [ ] `runtime/context.py` — `_runtime_guidance()`: use `_guidance_registry` list; add `register_guidance()` classmethod

**Acceptance criteria**:
1. `_BuildState` replaces 30-param `_build_report()` — new params added in one place
2. Instruction files read from disk only when mtime changes; 2nd+ read in same session hits cache
3. Plugin can call `ContextBuilder.register_guidance("Use parallel_tasks ...")` at init time

---

## P7: Testing

**Files to create**:
- [ ] `tests/test_context_budget.py` — budget enabled by default, section truncation, keep_recent_results
- [ ] `tests/test_token_estimator.py` — estimate_tokens accuracy, emergency_trim preserves head+tail
- [ ] `tests/test_context_instruction_cache.py` — mtime-based cache hit/miss
- [ ] `tests/test_reflection_rate_limit.py` — interval-based checking, immediate triggers still work

**Files to modify**:
- [ ] `tests/test_context_instructions.py` — verify instruction files loaded from cache
- [ ] `tests/test_pipeline_tool_loop_guard.py` — verify no regression after compact.py removal

**Acceptance criteria**:
1. All new test files pass
2. All existing test files pass (no regression)
3. End-to-end test: 30-turn conversation → ContextBuilder.build() → messages fit in budget → no mutation on session
4. End-to-end test: reflection at steps 10, 15, 20; immediate at step 7 (tool error)

---

## Verification Checklist (per phase, before marking complete)

Each phase:
- [ ] Code passes `python -c "from module import *"`
- [ ] Code passes `ruff check`
- [ ] Relevant tests pass
- [ ] Manual smoke test
- [ ] git commit with Conventional Commits message

Gate before P1:
- [ ] P0 merged: no `from runtime.compact import ...` anywhere
- [ ] `micro_compact` / `auto_compact` no longer called from Pipeline

Gate before P7:
- [ ] All P0–P6 code on branch
- [ ] No regression in existing test suite
- [ ] `.env.example` updated with new defaults

---

## Rollback Plan

Each phase is independently revertible:

- **P0**: Revert `pipeline.py` compact calls, restore `compact.py` from git. Budget layer is additive.
- **P1**: Set `CONTEXT_ENABLE_SECTION_BUDGET=false` to restore old behavior.
- **P2**: `_emergency_trim` is a no-op when messages fit; comment out the guard.
- **P3**: Set `REFLECTION_INTERVAL=1` to restore per-step checking.
- **P4**: Context frame ordering is cosmetic — revert to old flat format.
- **P5**: Revert `_summary_message()` to old flat format.
- **P6**: `_BuildState` is internal to ContextBuilder; instruction cache has no external effects.
