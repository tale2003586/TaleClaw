# Streaming UX and Completion Repair

## Root Causes

The 4-5 second completion delay was in `AgentService._ask_async()`: it awaited title generation after the critical turn had completed but before returning the reply to the SSE worker. A controlled 4.2 second title model measured 4214 ms from last delta to `complete`, 4204 ms of which was title generation.

Coding intermediate content was dropped at the application boundary. `TurnCoordinator` called `CodingApplication.run_coding_task()` without `on_text`, and `CodingApplication` built its `RunContext` without it. Provider and `ReasoningLoop` streaming already worked, but no Web callback reached the nested Coding runtime. The coordinator later emitted only the formatted parent reply once.

## Event Flow

Before:

```text
provider chunks -> nested ReasoningLoop (no Web callback)
-> tools -> final formatted reply -> one Web delta
-> critical finalize -> title model -> SSE complete
```

After:

```text
provider chunk -> Web delta
model response boundary -> assistant_segment
-> tool start/completion -> next model chunks
-> final segment -> assistant_completed
-> critical report/plugins/session save/outbound -> turn.completed / SSE complete
-> managed background title task
```

Existing `delta`, `thinking`, and `complete` events remain compatible. `assistant_segment` marks each model-response boundary and says whether it is progress or final. `assistant_completed` is the runtime fact that final visible output ended. Existing `complete` remains the critical turn-finalized boundary.

## Critical Path

| Step | Before assistant completed | Before next turn | Background eligible |
| --- | --- | --- | --- |
| model output and tool side effects | yes | yes | no |
| TaskState/checkpoints | yes | yes | no |
| final Session persistence | no | yes | no |
| trace/run report and required plugins | no | yes | no |
| outbound delivery / `complete` assembly | no | yes | no |
| session title model call | no | no | yes |
| optional metrics already embedded in trace | no | yes | not changed |

Title work is managed by `AgentService`: exceptions are consumed and logged, duplicate tasks per Session are suppressed, shutdown cancels and gathers tasks, and deletion cancels the corresponding title task. The task snapshots the Session under its lock, generates outside the lock, then conditionally commits only the title under the lock. This avoids blocking a second turn and prevents stale snapshots from replacing messages or an existing title.

## Frontend State

The chat hook now uses `streaming -> finalizing -> idle`. `assistant_completed` stops the generating presentation and enters `finalizing`; existing `complete` commits the persisted message and clears the transient streaming copy. Input stays disabled during critical finalization to preserve Session ordering, but title generation no longer participates in that interval. Progress segments are separated by blank lines and the formatted Coding parent reply is not replayed as another delta.

`thinking` remains a separate provider-controlled channel. Only explicit provider `reasoning_content` follows that path; no hidden chain-of-thought is synthesized or exposed. Tool and subagent trace projection stays whitelisted, so raw context, policy, checkpoints, state patches, and stack traces are not dumped into chat.

## Cancellation and Errors

Cancellation is checked at the next safe loop boundary. Already emitted visible chunks remain; the runtime emits a terminal assistant message and `assistant_completed(reason=user_cancelled)`, persists the stopped run, then sends normal turn completion. The frontend identifies the stopped finalization state. Title tasks are scheduled only after the critical turn returns and cannot delay Stop completion.

Title failure is isolated to logging and fallback behavior and cannot convert a successful answer to failure. Critical persistence/report/outbound failures retain existing error semantics and are not hidden or moved to background work.

## Verification

- Deterministic Coding test: three streaming model calls, two tool rounds, strict text/tool/final ordering, no duplicate final delta.
- Runtime cancellation test: cancellation after the first tool, visible progress retained, terminal reason `user_cancelled`, no second model call.
- SSE endpoint test: transports `assistant_segment` and `assistant_completed` before compatible `complete`.
- Frontend test: ordered progress/final text, tool activity, `streaming -> finalizing -> idle`, no duplicate committed reply.
- Title lifecycle tests: reply returns before a gated title; title success persists later; failure does not fail the reply; timeout fallback persists; shutdown/deletion management remains bounded.
- Real provider probe: `deepseek-v4-flash`, 8 streamed chunks before the tool and 16 streamed final chunks afterward.
- Controlled timing: 4214 ms before versus 10.420 ms after for last delta to backend complete.

## Required Answers

1. The old 4-5 second gap was the awaited session-title model call after critical turn completion.
2. Yes, title generation was on the user-response critical path.
3. It now starts as a managed task only after the reply and critical turn completion are available.
4. No. Title failure is logged/fallback-isolated and does not fail a completed answer.
5. Coding content was disconnected between `TurnCoordinator`, `CodingApplication`, and the nested `RunContext` callback.
6. Yes. Every model response's visible content uses the same streaming callback and emits a segment boundary.
7. Yes. A segment is published before its tool calls execute; deterministic tests assert the strict order.
8. Yes. The provider sends `stream=True`; both unit evidence and the real Coding probe confirm it.
9. No for streaming-capable providers. Chunks are forwarded directly. Non-stream providers retain the explicit whole-response compatibility path.
10. Yes. `assistant_completed` is visible-response completion; `complete`/`turn.completed` is critical turn finalization.
11. `assistant_completed` ends the generating presentation and enters `finalizing`; `complete` ends critical finalization and returns to idle.
12. Yes. A real Runtime cancellation test verifies retained progress, stopped state, terminal fact, and no subsequent model call.
13. Projected `subagent.started`/`subagent.completed` and tool activity provide deterministic feedback; child trace internals are not dumped into chat.
14. No. Only user-visible assistant content and explicitly provider-supplied thinking use display channels.
