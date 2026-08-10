# TaleClaw Runtime System Design

Date: 2026-08-04

This document describes the closed runtime architecture after the Runtime
Architecture Closure migration. It is intentionally limited to the execution,
state, context, tool and model-capability boundaries. TaskPlan and a new
Sandbox lifecycle remain future extension points and are not implemented here.

## Execution flow

```text
Inbound adapter (CLI/Web/Telegram/Feishu)
        |
        v
AppRuntime.run_message / run_once
        |
        v
TurnCoordinator
  - loads Session and records the inbound message
  - runs plugin hooks and ApplicationRouter
  - creates RunState and RunContext
        |
        +--> Chat application --> Runtime.run
        |
        +--> CodingApplication --> task Runtime.run
                                      |
                                      v
                                  AgentRunner
                                      |
                                      v
                                  ReasoningLoop
                                      |
                                      +--> ContextBuilder and contributors
                                      +--> model provider
                                      +--> ToolExecutor / ToolRegistry
```

`Runtime.run(agent, user_input, context)` is the only public Runtime execution
entry. `AgentRunner.run` is the internal adapter and `ReasoningLoop.run` is the
only model/tool state machine. Product-specific routing and lifecycle code is
owned by `applications/**`; `runtime/**` has no import dependency on it,
gateways or the Web server.

## State boundaries

| State | Durable authority | Contents | Explicit exclusions |
| --- | --- | --- | --- |
| `Session` | `SessionStore` | Messages, selected mode, immutable context events, resource references | Per-run counters and stop metadata |
| `TaskState` | Coding TaskState reducer/service | Objective, constraints, progress, findings, blockers, remaining work, completion basis and version | Tool fingerprints, retry caches, token use and compaction generation |
| `RunExecutionState` | Run lifetime and trace | Steps, usage, active calls, web budget, duplicate fingerprints, recovery attempts, cancellation, stop decision | Cross-run task meaning |
| `Trace/Event` | TraceStore and Session event log | Immutable tool/model/state/recovery/stop facts | Mutable prompt transport |
| `ContextSnapshot` | `context_snapshots` store table | Compressed summary, covered event range, source TaskState version and lifecycle | Task semantic mutations |
| `ToolSpec` | Tool registry instance | Schema, handler, mode, risk, idempotence, side effect, injection, scope and audit metadata | Runtime policy/governance lookups outside the spec |

Session metadata is limited to non-core extension data and legacy migration
input. New runs do not write `active_run_id`, `last_run_id`, stop strings or
execution counters there. Legacy WorkingMemory and CodingContextState payloads
are read one way by migration code and are removed from new writes.

## Context construction

`ContextBuilder.build_prefix` and `ContextBuilder.build` are the formal context
contract. Runtime passes trace and budget information as explicit keyword
arguments. Applications provide `ContextContributor` implementations through
`RuntimeExtensions`; the kernel never imports Coding, BUS or background result
types. Ordinary bot chat uses the shared builder without creating TaskState or
Coding context state. It also skips task/run checkpoints when the model returns
a normal one-step answer. Coding and teammate modes opt into their task-state
rendering, checkpoints and event-window compaction.

The coding view is an immutable `CodingContextSnapshot` projection. Its
renderer does not decide completion or mutate task semantics. `EventCompactor`
only summarizes events and never creates or applies a `StatePatch`.

## Snapshot lifecycle

Snapshots use a two-phase transaction:

```text
REQUESTED/GENERATING -> PREPARED -> ACTIVE -> (archive_completed)
                              \-> FAILED_RETRYABLE / FAILED_FINAL
```

1. Events and deterministic TaskState facts are flushed first.
2. The compactor creates a bounded summary with the source TaskState version.
3. The summary is persisted as `PREPARED`; it is not visible to prompts.
4. The prepared row is read and validated, then the active pointer is switched.
5. Only an `ACTIVE` snapshot may advance the archive boundary.

Snapshot ids are content addressed by session, source event range and source
TaskState version, so retrying the same range is idempotent. Activation failure
restores the prior active pointer and does not archive. Archive failure leaves
the valid active snapshot in place; startup recovery retries archive or activates
the newest prepared row. On reload, an archived active snapshot restores the
archive boundary even when no legacy checkpoint exists.

## Compaction failure ladder

`EventCompactor` has hard limits for normal, repair and chunked attempts. The
repair prompt includes concrete validation errors; chunking respects event
boundaries. If provider calls remain invalid or unavailable, deterministic
fallback retains the objective, constraints, pending work, findings, decisions,
blockers, evidence/artifact references and recent covered events. A failed
TaskState patch is recorded as an error but cannot roll back a valid snapshot;
a snapshot failure cannot modify TaskState.

## Recovery and stopping

`RecoveryController` receives anomaly facts from the execution loop. Repeated
non-idempotent or side-effecting calls stop immediately with
`REPEATED_SIDE_EFFECT_RISK`. A read-only/idempotent duplicate may invoke one
no-tool `RecoveryJudge`, followed by at most one corrected execution. The
controller validates the resulting action against the registered `ToolSpec` and
the incident budget. Incident identity includes normalized tool arguments,
error type, result hash and TaskState version. A run may invoke at most two
recovery judges in total. A second occurrence of the same incident, an invalid
decision, or a judge failure stops deterministically.

All stop paths use `StopDecision` and `StopReason`: `completed`,
`user_cancelled`, `waiting_user`, `hard_budget_exceeded`, `security_blocked`,
`tool_unavailable`, `non_retryable_failure`, `repeated_side_effect_risk`,
`no_progress`, `recovery_rejected`, `recovery_exhausted`,
`compaction_failed_final` and `partial_result_accepted`.

## Thinking capability contract

The single field is `thinking_enabled`, defaulting to `false`. A
`ModelProfile` declares `supports_thinking` and an explicit `thinking_param`.
OpenAI-compatible chat-completions and Responses adapters, including streaming
and tool requests, add that provider-specific parameter only when both the
capability and the request flag are true. Unknown relays receive no extra
field; profiles claiming support without a parameter are rejected at load time.

The Web API validates the optional boolean and returns HTTP 400 when enabled
for an unsupported profile. `/api/health` exposes `thinking_supported`; the
React chat control is disabled from that capability and never displays hidden
chain-of-thought. Logs may record the boolean capability decision but not
private reasoning content.

## Fault-injection matrix

| Fault | Expected result |
| --- | --- |
| Normal summary invalid | One repair request with validation errors |
| Repair invalid or input too large | One chunked attempt, then deterministic fallback |
| Provider unavailable | Original events remain; active snapshot unchanged or fallback activates |
| PREPARED save failure | No active pointer or archive change |
| Activation failure | Old active snapshot remains; PREPARED is recoverable |
| Archive failure | Active snapshot remains valid; archive retry is idempotent |
| Process restart | Prepared/active rows are scanned and recovered deterministically |
| Duplicate read-only call | One no-tool judge and at most one correction |
| Duplicate side-effect call | Immediate standard stop; no judge or retry |
| User cancellation or hard budget | Immediate standard stop |

## Future boundaries

Sandbox integration should attach resource references and policy contributors at
the application boundary; it must not add sandbox lifecycle state to Runtime or
TaskState. A future TaskPlan can be represented as additional semantic fields
and reducer events in TaskState, while execution limits and scheduling remain
in RunExecutionState. Neither boundary requires changing `Runtime.run`.
