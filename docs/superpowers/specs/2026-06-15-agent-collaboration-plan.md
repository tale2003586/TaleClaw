# Agent Loop & Multi-Agent Collaboration Work Plan

**Date**: 2026-06-15
**Design doc**: [2026-06-15-agent-collaboration-design.md](2026-06-15-agent-collaboration-design.md)

## Phase Summary

| Phase | Scope | Est. effort | Priority | Depends on |
|-------|-------|-------------|----------|------------|
| P0: AgentLoop Refactor | Decompose `run_once()` into 7 phase methods | 0.5 day | P0 | — |
| P1: Short-Lived Subagent | `TaskSubagentRunner` + wire `task` tool handler | 1.5 days | P0 | P0 |
| P2: Structured Protocol | `AgentMessage`, `MessageType`, `ReliableMessageBus` | 1.5 days | P0 | — (independent) |
| P3: Protocol Integration | Teammate handlers + error propagation + plan handshake | 1.5 days | P1 | P1 + P2 |
| P4: Fan-out | `parallel_tasks` tool + thread-level concurrency | 0.5 day | P1 | P1 |
| P5: Testing | Integration + end-to-end collaboration scenario tests | 1 day | P1 | P0–P4 |

---

## Dependency Map

```
P0 (AgentLoop refactor) ──→ P1 (Subagent) ──→ P3 (Protocol Integration) ──→ P5 (Testing)
                                                     │
P2 (Structured Protocol) ────────────────────────────┘
                                                     │
                                               P4 (Fan-out) ──→ P5
```

P0 and P2 are independent — can be developed in parallel.
P4 can be done any time after P1.
P3 requires both P1 and P2.
P5 depends on all previous phases.

---

## P0: AgentLoop Refactoring

**Files to modify**:
- [ ] `runtime/agent_loop.py` — decompose `run_once()` into `_receive`, `_preprocess`, `_route`, `_handle_switch`, `_record`, `_execute`, `_postprocess`, `_deliver`

**Files to create**:
- [ ] `tests/test_agent_loop_phases.py`

**Method extraction plan** (lines from current `run_once`, lines 31–172):

| New method | Lines extracted from | Responsibility |
|------------|---------------------|----------------|
| `_receive` | 32–36, 196–219 | inbound consume + identity check + run_state start |
| `_preprocess` | 39–63 | `plugin_manager.before_turn()` → abort decision |
| `_route` | 65–80 | `router.route()` → RouteResult |
| `_handle_switch` | 82–112 | mode switch: record + emit + publish + finish_run |
| `_record` | 114–119 | append user message to session |
| `_execute` | 121–152 | dispatch: task_session vs pipeline |
| `_postprocess` | 154–155 | `plugin_manager.after_turn()` |
| `_deliver` | 157–172 | finish_run + save + publish + error handling |

Plus: pull error handling (170–172) into `_fail_run` (already exists, lines 252–269).

**Acceptance criteria**:
1. All existing `test_agent_runner_*`, `test_pipeline_*`, `test_mode_switch` pass unchanged
2. `test_agent_loop_phases.py` covers each phase method independently
3. `AgentLoop.__init__` accepts `subagent_runner` parameter (default `None`, no-op if absent)
4. Each phase method ≤ 35 lines

---

## P1: Short-Lived Subagent System

**Files to create**:
- [ ] `agents/subagent/__init__.py`
- [ ] `agents/subagent/runner.py` — `TaskSubagentRunner`, `SubagentResult`
- [ ] `agents/subagent/tools.py` — `SUBTASK_TOOL_WHITELIST`, `SUBTASK_SYSTEM_PROMPTS`

**Files to modify**:
- [ ] `tools/handlers.py:771-773` — replace stub with `_run_subagent_task(...)` handler
- [ ] `tools/handlers.py` — add `_run_subagent_task` function (or import from `agents/subagent/runner`)
- [ ] `runtime/bootstrap.py` — instantiate `TaskSubagentRunner`, inject into `AgentLoop`
- [ ] `runtime/agent_loop.py` — store `self.subagent_runner`, pass to `_execute`

**Files to create (tests)**:
- [ ] `tests/test_subagent_runner.py`

**Implementation steps**:

1. Create `agents/subagent/tools.py` with whitelists and system prompts
2. Create `agents/subagent/runner.py` with `TaskSubagentRunner`:
   - `run(prompt, agent_type, parent_session)` → `SubagentResult`
   - Creates isolated `Session`, filtered `ToolRegistry`, forked `Pipeline`
   - Calls `sub_pipeline.run()`, extracts result from session messages
3. Wire handler in `tools/handlers.py`:
   - `"task": lambda **kw: _format_subagent_result(subagent_runner.run(...))`
4. Bootstrap: instantiate `TaskSubagentRunner`, pass to `AgentLoop`
5. AgentLoop: store `subagent_runner`, pass to tool handler context

**Acceptance criteria**:
1. `TaskSubagentRunner.run(prompt="List all .py files", agent_type="explore")` returns `SubagentResult(success=True, ...)` with file listing in summary
2. `TaskSubagentRunner.run(prompt="Delete all files", agent_type="explore")` — explore agent has no write tools, returns summary indicating limitation
3. `code` agent can write files; `plan` agent cannot
4. No recursion: subagent tool registry has no `task` or `spawn_teammate` tools
5. Lead agent calling `task(agent_type="explore", prompt="...")` receives formatted tool_result with subagent summary
6. All existing tests pass (no regression)

---

## P2: Structured Agent Communication Protocol

**Files to create**:
- [ ] `bus/protocol.py` — `MessageType` enum, `AgentMessage` dataclass, payload type hints
- [ ] `bus/reliable.py` — `ReliableMessageBus`
- [ ] `tests/test_bus_protocol.py`

**Files to modify**:
- [ ] `bus/__init__.py` — add exports

**Implementation steps**:

1. Define `MessageType` enum (10 types)
2. Define `AgentMessage` dataclass with `to_json()` / `from_json()`
3. Define payload type hints per `MessageType` (docstrings or TypedDict)
4. Implement `ReliableMessageBus`:
   - `request()` — send + block until response
   - `respond()` — reply with correlation_id
   - `notify_arrival()` — wake waiting threads
   - Thread-safe with `threading.Event` + `threading.Lock`
5. Backward-compat: `AgentMessage.from_json()` handles old `{sender, content, type}` format

**Acceptance criteria**:
1. `AgentMessage.to_json()` → `from_json()` round-trips correctly
2. `ReliableMessageBus.request()` returns `AgentMessage | None` (timeout returns `None`)
3. `ReliableMessageBus.respond()` correctly correlates to a waiting `request()`
4. Two threads: thread A calls `request()`, thread B calls `respond()` → thread A unblocks and returns response
5. Old-format `BUS.send(...)` / `BUS.read_inbox(...)` still works unchanged
6. `AgentMessage.from_json({"sender":"a","content":"hi","type":"message"})` doesn't crash (graceful fallback)

---

## P3: Protocol Integration into Teammates

**Files to modify**:
- [ ] `tools/handlers.py` — update `send_message`, `spawn_teammate`, `plan_approval`, `shutdown_request`, `shutdown_status`, `read_inbox` handlers to use `AgentMessage`
- [ ] `coding_runtime/teammate.py` — error propagation (traceback → `MessageType.ERROR` to lead), structured inbox rendering via `TeammateContextBuilder`
- [ ] `coding_runtime/protocols.py` — add `ReliableMessageBus`-based plan approval + shutdown flow with proper request-response

**Files to create**:
- [ ] `tests/test_teammate_protocol.py`

**Implementation steps**:

1. Update `send_message` handler — wraps payload in `AgentMessage` before sending
2. Update `spawn_teammate` handler — sends `TASK_ASSIGN` instead of raw prompt
3. `TeammateContextBuilder._render_message()` — formats `AgentMessage` as readable LLM context with type-specific labels
4. Error propagation — `_run_member` catch block sends `MessageType.ERROR` to lead inbox
5. Plan approval — teammate uses `ReliableMessageBus.request()` and blocks for `PLAN_RESPONSE`
6. Shutdown — same pattern with `SHUTDOWN_REQUEST` / `SHUTDOWN_RESPONSE`
7. Teammate handler `plan_approval_request` — starts blocking wait for lead response (with timeout fallback)

**Acceptance criteria**:
1. Lead spawns teammate → teammate receives `TASK_ASSIGN` message (structured, not raw string)
2. Teammate crashes → lead inbox contains `ERROR` message with traceback
3. Teammate sends `PLAN_REQUEST` → lead approves via `plan_approval` → teammate receives `PLAN_RESPONSE` and continues
4. Old-format messages still render correctly in both lead and teammate inboxes
5. `read_inbox` for lead shows messages grouped by type in human-readable format
6. No regression: `test_teammate_reasoning_loop.py` passes

---

## P4: Fan-out Enhancement

**Files to create**:
- [ ] `agents/subagent/parallel.py` — `run_parallel_tasks`

**Files to modify**:
- [ ] `tools/schema.py` — add `parallel_tasks` to `LEAD_ONLY_TOOLS`
- [ ] `tools/handlers.py` — wire `parallel_tasks` handler

**Acceptance criteria**:
1. `parallel_tasks(tasks=[{agent_type:"explore", prompt:"Find auth files"}, {agent_type:"explore", prompt:"Find db files"}])` runs both concurrently
2. Results returned as JSON array of `SubagentResult` dicts
3. `maxItems: 8` enforced at schema level
4. Timeout: individual tasks timeout at 300s, not blocking forever
5. Thread safety: no shared state corruption when running multiple subagents

---

## P5: Testing & Verification

**Files to create**:
- [ ] `tests/test_agent_loop_phases.py` — unit tests per phase method
- [ ] `tests/test_bus_protocol.py` — `AgentMessage` serialization + `ReliableMessageBus` threading
- [ ] `tests/test_subagent_runner.py` — explore/plan/code agent types
- [ ] `tests/test_teammate_protocol.py` — end-to-end spawn → assign → result → error
- [ ] `tests/test_parallel_tasks.py` — concurrent subagent execution

**Acceptance criteria**:
1. All new test files pass
2. All existing test files pass (no regression)
3. End-to-end scenario test: lead spawns explore subagent → receives file list → spawns code subagent → file is modified → lead verifies modification
4. End-to-end protocol test: lead spawns teammate → assigns task → teammate completes and reports result → lead reads structured result
5. End-to-end error test: teammate crashes → lead receives error with traceback without hanging

---

## Verification Checklist (per phase, before marking complete)

Each phase:
- [ ] Code passes `python -c "from module import *"`
- [ ] Code passes `ruff check`
- [ ] Relevant unit tests pass
- [ ] Manual smoke test on 1-3 real scenarios
- [ ] git commit with Conventional Commits message

Gate before P3:
- [ ] P0 and P1 merged (subagent works end-to-end)
- [ ] P2 merged (protocol types + reliable bus work)

Gate before P5:
- [ ] All P0–P4 code on branch
- [ ] No regression in existing test suite
- [ ] `AgentLoop` with `subagent_runner=None` still works (backward compat)

---

## Rollback Plan

Each phase is independently revertible:

- **P0**: If AgentLoop decomposition introduces bugs, revert `agent_loop.py` to original. No other code depends on new method signatures.
- **P1**: If subagent runner has issues, set `subagent_runner=None` in `bootstrap.py`. The `task` tool stub is restored. No other code path affected.
- **P2**: Protocol files are additive. If `ReliableMessageBus` has threading issues, existing `MessageBus` remains untouched and all callers still work.
- **P3**: Protocol integration can be rolled back by reverting handler changes. Old ad-hoc handlers still function through backward-compat path in `AgentMessage.from_json()`.
- **P4**: `parallel_tasks` is an additive tool. Disable by removing from `LEAD_ONLY_TOOLS`.

---

## Implementation Status

**Status**: Implemented and verified on 2026-06-15.

### Completed

- **P0 AgentLoop Refactor**: `runtime/agent_loop.py` is decomposed into `_receive`, `_preprocess`, `_route`, `_handle_switch`, `_record`, `_execute`, `_postprocess`, and `_deliver`. Each phase is <= 35 lines.
- **P1 Short-Lived Subagent**: `TaskSubagentRunner` is wired through bootstrap and the lead `task` tool. Unknown `agent_type` now returns a structured failure instead of silently falling back.
- **P2 Structured Protocol**: `AgentMessage`, `MessageType`, and `ReliableMessageBus` are available and backward-compatible with legacy inbox wrappers.
- **P3 Teammate Protocol Integration**: `spawn_teammate`, `send_message`, `broadcast`, plan approval, shutdown, inbox rendering, and teammate error propagation use structured protocol messages.
- **P4 Fan-out**: `parallel_tasks` runs bounded subagent tasks concurrently, preserves result order, enforces an 8-task cap, and returns structured timeout/error results.
- **P5 Tests**: Added or expanded tests for agent loop phases, subagent runner behavior, reliable protocol, teammate protocol, and parallel fan-out.

### Verification

- `python -m pytest -q`: `226 passed`
- `python -m py_compile runtime/agent_loop.py agents/subagent/runner.py agents/subagent/tools.py agents/subagent/parallel.py bus/protocol.py bus/reliable.py coding_runtime/protocols.py coding_runtime/teammate.py tools/handlers.py`: passed

### Design Note

Plan and shutdown flows use structured messages with `correlation_id` and inbox notification. They do not hard-block inside the teammate tool call, because the current inbox reader is also the mechanism that wakes pending protocol messages; a hard block without an independent poller can deadlock. The protocol layer still supports blocking request/response for callers that can provide an external notifier.
