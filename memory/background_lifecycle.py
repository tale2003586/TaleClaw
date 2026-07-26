from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
import threading
from typing import Any

from runtime.trace.summary import write_trace_summary


class BackgroundMemoryLifecycle:
    """Run memory lifecycle work outside the user-facing response path."""

    def __init__(self, lifecycle, *, max_workers: int = 1) -> None:
        self.lifecycle = lifecycle
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)),
            thread_name_prefix="memory-lifecycle",
        )
        self._futures: list[Future] = []
        self._lock = threading.Lock()

    def enqueue_after_turn(
        self,
        session,
        *,
        run_state=None,
        trace_store=None,
    ) -> Future:
        session_snapshot = deepcopy(session)
        future = self._executor.submit(
            self._run,
            session_snapshot,
            run_state=run_state,
            trace_store=trace_store,
        )
        with self._lock:
            self._futures.append(future)
            self._futures = [item for item in self._futures if not item.done()]
        if trace_store is not None and run_state is not None:
            trace_store.append_event(run_state, "memory.lifecycle.scheduled", {
                "session_id": getattr(session, "id", ""),
                "message_count": len(getattr(session, "messages", []) or []),
            })
        return future

    def wait(self, timeout: float | None = None) -> bool:
        with self._lock:
            futures = list(self._futures)
        if not futures:
            return True
        done, pending = wait(futures, timeout=timeout)
        with self._lock:
            self._futures = [item for item in self._futures if not item.done()]
        return not pending

    def shutdown(self, *, wait_for_jobs: bool = True) -> None:
        self._executor.shutdown(wait=wait_for_jobs)

    def _run(self, session, *, run_state=None, trace_store=None) -> Any:
        try:
            result = self.lifecycle.after_turn(session)
        except Exception as exc:
            self._append_event(trace_store, run_state, "memory.lifecycle.failed", {
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })
            return None

        if result is not None:
            for item in getattr(result, "trace_events", []) or []:
                event_name = item.get("event")
                payload = item.get("payload") or {}
                if event_name:
                    self._append_event(trace_store, run_state, event_name, payload)
            self._append_event(
                trace_store,
                run_state,
                "memory.lifecycle.completed",
                result.to_trace_payload()
                if hasattr(result, "to_trace_payload")
                else {},
            )
            self._refresh_trace_summary(trace_store, run_state)
        return result

    def _append_event(self, trace_store, run_state, event_name: str, payload: dict) -> None:
        if trace_store is None or run_state is None:
            return
        trace_store.append_event(run_state, event_name, payload)

    def _refresh_trace_summary(self, trace_store, run_state) -> None:
        if trace_store is None or run_state is None:
            return
        try:
            write_trace_summary(trace_store.run_dir(run_state))
        except Exception:
            return
