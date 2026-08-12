# Streaming Event Timeline

## Scope

Measured on HEAD `13f6ee00b63ef5ebb63205b868978e4cc709ae49` with a controlled Web request and a 4.2 second title provider. The same critical persistence delay (about 10 ms) was used before and after the repair. Times use `time.perf_counter()` from request receipt.

## Before

The original `_ask_async()` awaited `runtime.run_message()`, then awaited `_ensure_session_title()`, and only then returned the reply to the SSE worker.

| Event | Time |
| --- | ---: |
| request received | T+0.000 s |
| final assistant last delta | T+0.011 s |
| turn completed / critical save done | T+0.021 s |
| session title started | T+0.021 s |
| session title completed | T+4.225 s |
| backend `complete` ready | T+4.225 s |

`last visible delta -> complete` was **4214 ms**. Approximately **4204 ms** came from the title model call; critical finalization was about **10 ms**.

## After

| Event | Time |
| --- | ---: |
| request received | T+0.000 s |
| model call started | T+0.000 s |
| final assistant last delta | T+0.005 s |
| `assistant.completed` | T+0.005 s |
| `turn.finalize.started` | T+0.005 s |
| critical session save completed | T+0.016 s |
| `turn.completed` / backend `complete` ready | T+0.016 s |
| session title started | T+0.016 s |
| session title completed and saved | T+4.220 s |

`last visible delta -> complete` is now **10.420 ms**. The title finishes **4203.789 ms after** the reply is ready and no longer delays the answer.

## Coding Timeline

The deterministic three-model/two-tool test records this causal sequence:

```text
delta, delta, assistant segment(progress)
tool started, tool completed
delta, delta, assistant segment(progress)
tool started, tool completed
delta, delta, assistant segment(final)
assistant completed
turn finalize started
turn completed / SSE complete
```

The configured real Coding route was also probed with a read-only diagnostic tool on 2026-08-13:

| Metric | Observed |
| --- | ---: |
| selected profile/model | `deepseek_flash` / `deepseek-v4-flash` |
| first visible delta | T+2.662 s |
| model call 1 chunks before tool | 8 |
| tool boundary | T+2.824 s |
| model call 2 chunks | 16 |
| final visible delta | T+5.216 s |
| model call 2 complete | T+5.218 s |

Both calls used the provider streaming path. TaleClaw forwards chunks as received; it does not simulate character-by-character output after receiving a complete response.

## Trace Boundaries

The runtime now records `stream.first_delta`, `stream.last_delta`, `assistant.segment.completed`, `assistant.completed`, `turn.finalize.started`, and `turn.completed`. Managed title tasks log `session_title.started`, `session_title.completed`, or `session_title.failed`, including run/session identity and duration.
