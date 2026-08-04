# TaleClaw Runtime Architecture Pruning Audit

Date: 2026-08-04  
Scope: production Python runtime, Coding application, context persistence, tool registry, model routing, Web API and React console.

This document records the pre-change architecture observed in the repository. It is an audit, not a description of the intended end state. Final dispositions are recorded in `runtime-pruning-result.md`.

## 1. Actual runtime call chain

The external entry points are `cli.py`, `telegram_worker.py`, `feishu_worker.py`, and `web/server.py`. They call `runtime.bootstrap.build_runtime()`, which composes `AppRuntime`, the message bus, stores, model pool, tools, `Runtime`, `AgentLoop`, and `CodingApplication`.

The inbound path is:

1. `AppRuntime.run_message()` / `run_once()` in `runtime/app_runtime.py` passes an `InboundMessage` to `AgentLoop.run_inbound()` in `runtime/agent_loop.py`.
2. `AgentLoop.run_inbound()` externalizes long input, loads a `Session` through `SessionManager.get_or_create()`, starts a `RunState`, runs plug-in preprocessing, and calls `AgentRouter.route()`.
3. `AgentLoop._record()` appends the user message and immutable context event.
4. `AgentLoop._execute()` either calls `CodingApplication.run_coding_task()` or `Runtime.run()`.
5. Coding creates an isolated task session in `applications/coding/runner.py`, initializes Coding `TaskState`, forks the base runtime, then also calls `Runtime.run()`.
6. `Runtime.run()` delegates through the legacy `run_turn()` and `_run_turn()` methods to `AgentRunner.run_turn()`.
7. `AgentRunner` creates the sole `ReasoningLoop`; `ReasoningLoop.run()` repeatedly assembles context, invokes the selected model through `runtime.execution.model_invocation.invoke_model()`, executes calls through `ToolExecutor` and `ToolRegistry`, appends assistant/tool messages and immutable events, and applies loop guards.
8. `Runtime._after_turn()` runs memory lifecycle work. `AgentLoop._deliver()` finalizes trace/run state, persists the session, and publishes the outbound message.

The model/tool loop itself is unique, but its surrounding entry and state setup are not yet closed.

## 2. Runtime entry audit

| Entry | Location | Real callers before pruning | Finding |
| --- | --- | --- | --- |
| `Runtime.run` | `runtime/runtime.py` | `AgentLoop`, `CodingApplication`, subagent runner | Intended public entry. |
| `Runtime.run_turn` | `runtime/runtime.py` | Internal `Runtime.run`, many legacy tests | Compatibility public entry; no production caller needs it. |
| `Runtime._run_turn` | `runtime/runtime.py` | `run_turn`, legacy tests | Compatibility implementation with a second callable signature. |
| `AgentRunner.run_turn` | `runtime/execution/agent_runner.py` | `Runtime`, teammate runner, tests | Internal protocol, but still named as a public turn entry. |
| `ReasoningLoop.run` | `runtime/execution/reasoning_loop.py` | `AgentRunner` | The actual model/tool state machine. |

`runtime/runtime.py`, `runtime/agent_loop.py`, `applications/coding/compaction.py`, and `runtime/execution/reasoning_loop.py` use `inspect.signature` to probe optional parameters. The Runtime and coordinator probes are historical signature compatibility, not a domain requirement. `AgentLoop` also stores both `pipeline` and `runtime` references to the same object.

## 3. State audit

### Session

`runtime/sessions/session.py::Session` owns chat messages, selected agent, immutable context events, active event window, prompt transport, archive boundary and checkpoints. `SessionStore` persists those to `sessions`, `messages`, `context_events`, and `context_checkpoints`.

Before pruning, `Session.metadata` also carries authoritative or duplicated run facts: active/last run ids, stop reason/message, web-search counters, finishing-reminder state, security-context use, WorkingMemory, TaskState, CodingContextState, tool unlocks, and context metrics. The first six overlap `RunState`/`RunExecutionState`.

### TaskStateCore and Coding TaskState

`runtime/task_state/models.py::TaskStateCore` is the shared semantic state. `applications/coding/task_state.py::TaskState` subclasses it with rich Coding facts. Both load from checkpoints and `Session.metadata["task_state"]`; both write through service functions. Patch validation provides optimistic version checking and rejects illegal terminal transitions.

The Coding extension additionally stores `ExecutionMemory` (`observed_tools`, fingerprints, hashes, step checkpoints, last step, and compaction generation). These are run/control facts and are a second copy of data already present in events and run state.

### RunExecutionState and RunState

`runtime/runtime.py::RunExecutionState` stores per-run input/messages, stop strings, security-context use, finishing reminder, web-search counters and usage. `runtime/trace/run_state.py::RunState` separately stores reasoning steps, tool calls, last tool, status and stop reason. Both are created for a turn, but policies fall back to Session metadata and therefore double-write counters and stop state.

### WorkingMemory

`runtime/working_memory.py` creates, loads, saves, checkpoints, completes and synchronizes WorkingMemory. Production imports existed in the coordinator, Coding application, ContextBuilder, loop policies, subagent runner/parallel execution, tool handlers and bootstrap. It can project from TaskState and can write observations/checkpoints back into Coding `TaskState.execution_memory`. The feature flags are `WORKING_MEMORY_CHECKPOINT_ENABLED` and `WORKING_MEMORY_RESUME_ENABLED`.

Useful legacy data are objective, completed/pending descriptions and evidence references. They already have TaskState equivalents. Tool observations and checkpoints belong in events/run state. Consequently WorkingMemory has no remaining valid production write responsibility; only a one-way legacy reader is justified.

### CodingContextState and checkpoints

`applications/coding/context_state.py::CodingContextState` is described as read-only but is rewritten into Session metadata after every context build. It combines renderer metadata, prompt-tail position, archive boundary, compaction generation and metrics. Checkpoint data live a second time in `Session.checkpoints`.

### Trace/Event

`Session.append_event()` owns immutable context facts; `TraceStore.append_event()` owns operational run/model/tool facts. Both are monotonic and are the durable evidence for tool execution and state transitions. Tool results are also present in Session messages for provider transport.

## 4. Context and compaction audit

`runtime/context/builder.py::ContextBuilder.build()` is the effective full-prompt assembler. It composes prompt assets, history, memory, retrieval, task state, Coding state, attachments, runtime events and a final budget report. `Runtime._before_reasoning()` is a second orchestration surface that probes builder parameters and injects Coding background/team-bus data directly from the Runtime.

Coding pressure is evaluated inside `build_coding_context_view()`. When above the soft threshold it selects a recent complete message-group tail and calls `_compact_events()`. That function constructs `CompactionCoordinator` from `applications/coding/compaction.py`.

The pre-change compaction sequence is:

1. deterministically extract a `StatePatch` from old events;
2. optionally ask a summary model for another `StatePatch`;
3. apply and validate the patch against TaskState;
4. increment `TaskState.execution_memory.compaction_generation`;
5. build a `ContextCheckpoint` containing the resulting TaskState;
6. persist checkpoint, completion event and archive boundary in one operation.

This reproduces the reported failure: a semantic patch parse/validation/application error raises `CompactionError`, no checkpoint is committed, the old active window remains, and the next build can request the same semantic patch again. There is no PREPARED state, no activation-only retry, no bounded repair/chunk ladder, and no deterministic summary fallback. StatePatch success is therefore incorrectly a prerequisite for context compaction.

## 5. Tool system audit

The model schemas live in `tools/schema.py`; implementations are registered by `ToolRegistry`. Static metadata are duplicated across:

* `ToolSpec.risk`, `allowed_agents`, `always_on`, `session_scoped`, `admin_only`;
* `_risk_for_tool()` and `_modes_for_tool()` in `tools/tool_registry.py`;
* `SESSION_SCOPED_TOOLS` in the same file;
* `ALWAYS_ON_TOOLS`, `PRELOADED_TOOLS_BY_MODE`, and `DEFERRED_TOOLS` in `tools/policy.py`;
* `ToolGovernanceMetadata.risk_level`, `allowed_modes`, and state/context effects in `tools/governance.py`.

Registry, catalog and policy consequently can disagree. In particular `DEFERRED_TOOLS` does not drive visibility; absence from the preload table does. Risk is represented by two differently typed fields, and mode restrictions can exist in `allowed_agents` and governance.

## 6. Duplicate execution and stopping audit

`ToolLoopGuardHook` detects normalized repeated calls/results. `ReasoningLoop` recognizes guard denial and either calls the general reflection agent or stops immediately. Unavailable tools stop after two attempts. Empty model responses retry once. Reasoning steps have a hard maximum and a finishing reminder is injected at a configured ratio.

The pre-change detector does not include TaskState version in its incident fingerprint, does not distinguish read-only/idempotent tools from side-effecting tools when deciding recovery, and has no incident-scoped recovery budget. Reflection is a general loop collaborator with broad context rather than a no-tools, one-shot RecoveryJudge. Stop reasons only cover step limit, empty response, repeated tool, unavailable tool, cancellation and timeout; stop state is also written as free-form Session metadata.

## 7. Thinking capability audit

Model configuration is environment/JSON based in `models/model_pool.py::ModelProfile`. `build_model_pool_from_env()` reads `LLM_PROVIDERS_JSON`, per-provider environment variables and routes. There is no model CRUD API or model settings UI: `SettingsPage.tsx` only manages theme/workspace. `OpenAICompatibleProvider` supports Chat Completions and Responses, streaming and non-streaming, including tools.

No `thinking_enabled`, `thinking_mode`, `reasoning_effort`, or explicit thinking capability exists. A boolean is consistent with the existing boolean profile settings. Safe mapping requires an explicit capability/parameter declaration per profile because arbitrary OpenAI-compatible relays must not receive unknown fields. The selected formal field for implementation is therefore `thinking_enabled: bool`, default false, accompanied by `supports_thinking` and an explicit provider parameter mapping.

## 8. Baseline tests

| Command | Result | Time/notes |
| --- | --- | --- |
| `.venv/bin/pytest -q` | 71 collection errors | 2.10 s. The executable entry point omitted the repository root from `sys.path`; modules such as `tests`, `web`, and `skill_runtime` could not be imported. Environment/startup issue, not behavioral failures. |
| `.venv/bin/python -m pytest -q --tb=short -x` | 122 passed, 1 skipped, then 1 failed | 20.01 s. First failure was `psycopg.OperationalError` opening the configured PostgreSQL test DSN. |
| `.venv/bin/python -m pytest ...` excluding PostgreSQL-dependent files | See final result document | Establishes the locally executable backend baseline independently of the unavailable external database. |
| `npm test -- --run` | 11 files / 26 tests passed | 3.67 s. |
| `npm run build` | passed | TypeScript and Vite build, 2.23 s. |
| `npm run lint` | passed | No ESLint diagnostics. |

No Python type-checker or linter is configured in `pyproject.toml`; compilation and the AST architecture contracts are used as the static backend checks.

## 9. Compatibility inventory

| Compatibility item | Old format/path | Target | Temporary reason | Deletion condition |
| --- | --- | --- | --- | --- |
| WorkingMemory | `metadata.working_memory` | TaskState plus events | Read old persisted sessions once | Delete reader after supported session retention window. |
| CodingContextState v1 | `metadata.coding_context_state` | ContextSnapshot | Recover old TaskState evidence only | Delete reader after supported session retention window. |
| Runtime entry | `run_turn`, `_run_turn` | `Runtime.run` | None for production | Remove in this closure. |
| pipeline alias | coordinator `.pipeline` | `.runtime` | None | Remove in this closure. |
| Session run metadata | budget/stop/step flags | RunExecutionState / RunState | Existing persisted metadata may be ignored | Stop all new writes now. |
| old model profile | missing thinking fields | `thinking_enabled=false` | Existing env/JSON configurations | Permanent read default; no reverse write. |
| task-state checkpoint | TaskState plus compaction generation | TaskState plus independent ContextSnapshot | Read old checkpoints for migration | New compactions only write snapshot format. |

## 10. Phased migration plan

1. Remove WorkingMemory and execution caches from normal task state, leaving a one-way legacy migration reader.
2. Make `Runtime.run` the sole facade entry; replace the outer AgentLoop with an application coordinator and eliminate Runtime product-layer imports/reflection-based signature compatibility.
3. Make ContextBuilder the single assembler and inject application context through a generic contributor.
4. Introduce independently persisted ContextSnapshots with bounded normal/repair/chunk/fallback generation, PREPARED/ACTIVE activation and deterministic recovery.
5. Add code-based anomaly detection, one-shot no-tool RecoveryJudge, deterministic controller validation and standardized stop decisions.
6. Make ToolSpec the sole consumed static metadata structure.
7. Keep simple Chat task-state-free and one-model-call when the model does not request tools.
8. Add boolean Thinking configuration through profile loading, capability mapping, adapters, API and UI.
9. Remove obsolete tests/aliases, update architecture documentation, and run all locally executable verification.
