# TaleClaw Event Loop Closure Diagnosis and Repair

## Baseline

- Date: 2026-08-10 (Asia/Shanghai)
- Branch: `feat/memory-runtime-evolution`
- Initial commit: `35326915a5dc817983d806c3ec4e09735d05d156`
- Initial commit summary: `3532691 Close runtime architecture migration`
- The initial worktree contained ten unrelated modified files. They were preserved.

## Incident Classification

The initial production excerpt contained only `RuntimeError: Event loop is
closed`. A subsequent full traceback established the first application error:

```text
TypeError: RoutedModelProvider.stream_chat() got an unexpected keyword argument
'thinking_enabled'
```

`invoke_model()` correctly supplied the per-run thinking flag, but
`RoutedModelProvider.stream_chat()` and `RoutedModelProvider.chat()` had not been
updated to accept and forward it to the selected concrete provider. The routed
streaming call therefore failed before entering the concrete model adapter.

`Event loop is closed` is a shutdown-stage secondary exception, not the primary
application error. This classification follows from the concrete lifecycle that
existed in `web/server.py`: shutdown awaited only the outbound dispatcher, called
`loop.stop()`, and immediately called `loop.close()` without owning, cancelling,
or awaiting request tasks and without joining the runtime thread. Any original
model, callback, cancellation, timeout, or network error could therefore be
followed and obscured by transport or task cleanup against the closed loop.

The routed provider signatures and `ModelPort` protocol now include the flag for
both streaming and non-streaming calls. The regression test
`test_thinking_flag_is_forwarded_for_chat_and_streaming` covers the exact failing
boundary. The lifecycle test `test_startup_error_is_not_replaced_by_cleanup_error`
reproduces that error ordering with `ModelError("first model failure")` followed
by `RuntimeError("secondary cleanup failure")` and verifies that the first error
remains the raised cause. This is a deterministic lifecycle reproduction, not a
claim that `ModelError` was the production exception.

## Root Causes

1. `AgentService` manually created and closed a loop but did not cancel/await
   cross-thread request coroutines, shut down async generators/default executor,
   or wait for the loop-owning thread to exit.
2. The Docker `CMD` runs `python web/server.py`. The Web process did not handle
   SIGTERM, so Docker stop could bypass the existing `KeyboardInterrupt` cleanup.
3. Telegram and Feishu workers also relied on default SIGTERM behavior rather
   than routing signals through their top-level asyncio lifecycle.
4. Feishu callback processing created unreferenced fire-and-forget tasks.
5. OpenAI-compatible chat-completions and Responses API streams were iterated
   without an explicit `close()` on normal completion, cancellation, or failure.
6. `AppRuntime.stop()` was not an explicit idempotent cancel-and-await operation.
7. Gateway HTTP clients and gateway close paths did not provide concurrent,
   idempotent close ownership.
8. The routed model adapter contract had drifted from `invoke_model()` and the
   concrete provider thinking-capability contract.

No `__del__` asynchronous cleanup exists. No internal nested `asyncio.run()` was
found. The remaining `asyncio.run()` calls are process or dedicated thread entry
points (`cli.py`, workers, VS Code script, and the Web runtime thread). No Redis,
async database pool, aiohttp session, WebSocket, MCP client, or async subprocess
resource exists in the audited production Python paths. Session/Postgres stores
are synchronous resources and retain explicit `close()` ownership.

## Ownership After Repair

```text
Web process / SIGTERM handler
  -> ThreadingHTTPServer stops accepting work
  -> AgentService rejects new submissions
  -> AgentService runtime-thread asyncio.run(_serve_async)
       -> cancel + await owned request tasks
       -> AppRuntime.stop
            -> cancel + await outbound_dispatch
       -> close SessionManager
       -> asyncio.run closes async generators/default executor
  -> join runtime thread
  -> close HTTP server socket
```

| Task/resource | Owner | Cancellation/close | Awaited |
| --- | --- | --- | --- |
| `outbound_dispatch` | `AppRuntime` | `AppRuntime.stop()` | yes |
| Web request coroutine | `AgentService._request_tasks` | `AgentService._stop_async()` | yes |
| Feishu callback event | `FeishuGateway._background_tasks` | `FeishuGateway.close()` | yes |
| Feishu HTTP server task | `FeishuGateway.run_forever()` | server shutdown | yes |
| Worker gateway/signal tasks | worker `main_async()` | SIGTERM/SIGINT | yes |
| Telegram `AsyncClient` | `TelegramBotApiClient` | locked idempotent `close()` | yes |
| Feishu `AsyncClient` | `FeishuApiClient` | locked idempotent `close()` | yes |
| Provider stream | `OpenAICompatibleProvider` call | scoped `finally` close | synchronous close completed |

Request disconnect now requests cancellation. Shutdown stops new Feishu event
creation before cancelling existing tasks. Cleanup attempts all owned resources
even if an earlier close fails. A cleanup error after an existing primary error
is logged in its narrow resource scope and cannot replace the primary error. No
error-message matching or global `RuntimeError` suppression was introduced.

## Modified Files

- `applications/app_runtime.py`: dispatcher ownership and idempotent stop.
- `web/server.py`: complete runtime-thread lifecycle, request ownership, cleanup
  ordering, disconnect cancellation, SIGTERM/SIGINT handling.
- `models/provider.py`: explicit ownership of both provider stream variants.
- `models/model_pool.py`, `runtime/ports.py`: aligned routed chat/streaming
  thinking arguments and the kernel model protocol.
- `gateway/telegram/adapter.py`, `gateway/feishu/adapter.py`: idempotent shutdown;
  Feishu background task registry.
- `gateway/telegram/client.py`, `gateway/feishu/client.py`: locked, idempotent
  `AsyncClient` close.
- `telegram_worker.py`, `feishu_worker.py`: loop-owned signal shutdown.
- `tests/test_async_runtime_lifecycle.py`: lifecycle regression coverage.
- `tests/test_model_pool_routing.py`: routed thinking argument regression.
- `docs/architecture/event-loop-closure-fix.md`: this report.

The unrelated pre-existing modifications under runtime/tool architecture docs
and tests were not changed as part of this repair.

## Tests

Executed successfully with asyncio debug and resource warnings enabled:

```bash
PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=default python -m pytest -q \
  tests/test_async_runtime_lifecycle.py \
  tests/test_model_pool_routing.py \
  tests/test_agent_loop_phases.py \
  tests/test_telegram_gateway.py::TelegramClientTests \
  tests/test_telegram_gateway.py::TelegramIdentityResolverTests \
  tests/test_feishu_gateway.py::FeishuIdentityResolverTests
# 38 passed

PYTHONASYNCIODEBUG=1 PYTHONWARNINGS=default python -m pytest -q \
  tests/test_recovery_controller.py \
  tests/test_runtime_architecture_closure.py \
  tests/test_tool_spec.py \
  tests/test_shared_runtime_task_state.py
# 24 passed

python -m py_compile applications/app_runtime.py web/server.py \
  models/provider.py gateway/telegram/client.py gateway/feishu/client.py \
  gateway/telegram/adapter.py gateway/feishu/adapter.py \
  telegram_worker.py feishu_worker.py

git diff --check
```

Result: 70 tests passed in the final combined run. No `Event loop is closed`, pending-task, unclosed-client,
unclosed-transport, or async-generator warning was emitted. New tests cover
double close, dispatcher cancellation/finally, Web request cancellation/finally,
HTTP client close, Feishu background task shutdown, provider stream close, and
preservation of the original exception when cleanup also fails.

The complete repository suite was not completed because `.env` points store
tests at PostgreSQL `127.0.0.1:55432`, which is unavailable in this environment.
A representative store test failed with `psycopg.OperationalError: connection is
bad`. This is an external test dependency failure, not a lifecycle assertion.

## Docker and Signal Verification

Docker runtime verification: NOT EXECUTED

Reason: Docker CLI 28.5.2 is installed, but access to `/var/run/docker.sock` is
denied. The required escalation was requested and rejected because the approval
review service returned HTTP 503. Container inspect, logs, build, start, request,
stop, and restart were therefore not claimed as verified.

A direct host start of the real Docker `CMD` entry point was also attempted with
`PYTHONASYNCIODEBUG=1`; the sandbox denied socket creation with `PermissionError:
[Errno 1] Operation not permitted`. Its escalation was rejected by the same
approval-service 503. Static verification confirms that Web, Telegram, and
Feishu process entry points now route SIGTERM through their owners; runtime
execution remains unverified in this restricted environment.

## Diff Stat

`git diff --stat` at report generation (untracked new test/report are not included
by Git until staged):

```text
22 tracked files changed, 704 insertions(+), 161 deletions(-)
```

This total includes the user's ten pre-existing modified files. The lifecycle
tracked-file delta also includes the routed thinking contract fix and regression
test; the two new untracked files are this report and
`tests/test_async_runtime_lifecycle.py`.

Event loop lifecycle fix: PASSED
