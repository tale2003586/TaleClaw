from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError, as_completed
import json
import time
from typing import Any

from agents.subagent.failure import (
    STATUS_FAILED,
    SubagentFailure,
    internal_error_failure,
    timeout_failure,
)
from agents.subagent.retry import annotate_retry_result, should_auto_retry
from config import WORKING_MEMORY_CHECKPOINT_ENABLED
from runtime.failure_reasons import StopReason
from runtime.working_memory import checkpoint_subtask_results


MAX_PARALLEL_SUBTASKS = 8


def run_parallel_tasks(
    *,
    runner,
    tasks: list[dict[str, Any]],
    parent_session=None,
    max_workers: int | None = None,
    timeout_seconds: float = 300,
    trace_store=None,
    parent_run_state=None,
    parent_span_id: str | None = None,
) -> list[dict[str, Any]]:
    bounded_tasks = list(tasks or [])[:MAX_PARALLEL_SUBTASKS]
    if not bounded_tasks:
        return []

    workers = max(1, min(max_workers or len(bounded_tasks), MAX_PARALLEL_SUBTASKS))
    results: list[dict[str, Any] | None] = [None] * len(bounded_tasks)
    run_specs: list[dict[str, Any]] = []
    executor = ThreadPoolExecutor(max_workers=workers)
    futures = {}
    try:
        for index, task in enumerate(bounded_tasks):
            run_kwargs = {
                "prompt": _compose_task_prompt(task),
                "agent_type": str(task.get("agent_type") or "explore"),
                "description": str(task.get("description") or ""),
                "parent_session": parent_session,
            }
            if trace_store is not None or parent_run_state is not None or parent_span_id:
                run_kwargs.update({
                    "trace_store": trace_store,
                    "parent_run_state": parent_run_state,
                    "parent_span_id": _subagent_span_id(parent_span_id, index),
                })
            run_specs.append(run_kwargs)
            futures[executor.submit(
                runner.run,
                **run_kwargs,
            )] = index

        started = time.monotonic()
        try:
            for future in as_completed(futures, timeout=max(0.001, timeout_seconds)):
                index = futures[future]
                results[index] = _result_from_future(future, bounded_tasks[index])
        except TimeoutError:
            pass

        timed_out = time.monotonic() - started >= timeout_seconds
        for future, index in futures.items():
            if results[index] is not None:
                continue
            if not future.done():
                future.cancel()
                failure = timeout_failure(timeout_seconds)
                results[index] = _error_result(
                    bounded_tasks[index],
                    f"TimeoutError: {failure.message}",
                    failure=failure,
                    truncated=True,
                    stop_reason=StopReason.TIMEOUT.value,
                )
                continue
            results[index] = _result_from_future(future, bounded_tasks[index])
        if timed_out:
            executor.shutdown(wait=False, cancel_futures=True)
        else:
            executor.shutdown(wait=True, cancel_futures=True)
    except Exception:
        executor.shutdown(wait=False, cancel_futures=True)
        raise

    retry_budget = len(bounded_tasks)
    for index, result in enumerate(list(results)):
        if retry_budget <= 0 or result is None:
            continue
        retried = _maybe_auto_retry(
            runner=runner,
            task=bounded_tasks[index],
            run_kwargs=run_specs[index],
            result=result,
            timeout_seconds=timeout_seconds,
        )
        if retried is not result:
            retry_budget -= 1
            results[index] = retried
            _trace_auto_retry(
                trace_store=trace_store,
                parent_run_state=parent_run_state,
                parent_span_id=parent_span_id,
                task_index=index,
                before=result,
                after=retried,
            )
    final_results = [item for item in results if item is not None]
    _checkpoint_parent_session(parent_session, bounded_tasks, final_results)
    return final_results


def _result_from_future(future, task: dict[str, Any]) -> dict[str, Any]:
    try:
        result = future.result()
        if hasattr(result, "to_dict"):
            return result.to_dict()
        if isinstance(result, dict):
            return result
        error = f"TypeError: unexpected result {type(result).__name__}"
        return _error_result(task, error, failure=internal_error_failure(error))
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        return _error_result(task, error, failure=internal_error_failure(error))


def _error_result(
    task: dict[str, Any],
    error: str,
    *,
    failure: SubagentFailure | None = None,
    truncated: bool = False,
    stop_reason: str | None = None,
    retry_count: int = 0,
) -> dict[str, Any]:
    failure = failure or internal_error_failure(error)
    return {
        "agent_type": str(task.get("agent_type") or "explore"),
        "success": False,
        "summary": "",
        "status": STATUS_FAILED,
        "files_touched": [],
        "tool_count": 0,
        "error": error,
        "truncated": truncated,
        "stop_reason": stop_reason,
        "findings": [],
        "incomplete": True,
        "failure_reason": failure.reason,
        "failure_message": failure.message,
        "recoverable": failure.recoverable,
        "retry_hint": failure.retry_hint,
        "evidence": failure.evidence,
        "retry_count": retry_count,
        "auto_retry": retry_count > 0,
        "recovered": False,
    }


def _maybe_auto_retry(
    *,
    runner,
    task: dict[str, Any],
    run_kwargs: dict[str, Any],
    result: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    if not should_auto_retry(result, retry_count=int(result.get("retry_count") or 0)):
        return result
    initial_failure_reason = str(result.get("failure_reason") or "")
    retried = _run_once_with_timeout(
        runner=runner,
        task=task,
        run_kwargs=run_kwargs,
        timeout_seconds=timeout_seconds,
        retry_count=1,
    )
    return annotate_retry_result(
        retried,
        retry_count=1,
        initial_failure_reason=initial_failure_reason,
    )


def _run_once_with_timeout(
    *,
    runner,
    task: dict[str, Any],
    run_kwargs: dict[str, Any],
    timeout_seconds: float,
    retry_count: int,
) -> dict[str, Any]:
    executor = ThreadPoolExecutor(max_workers=1)
    future = executor.submit(runner.run, **run_kwargs)
    try:
        result = future.result(timeout=max(0.001, timeout_seconds))
        executor.shutdown(wait=True, cancel_futures=True)
        if hasattr(result, "to_dict"):
            return result.to_dict()
        if isinstance(result, dict):
            return result
        error = f"TypeError: unexpected result {type(result).__name__}"
        return _error_result(
            task,
            error,
            failure=internal_error_failure(error),
            retry_count=retry_count,
        )
    except TimeoutError:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        failure = timeout_failure(timeout_seconds)
        return _error_result(
            task,
            f"TimeoutError: {failure.message}",
            failure=failure,
            truncated=True,
            stop_reason=StopReason.TIMEOUT.value,
            retry_count=retry_count,
        )
    except Exception as exc:
        executor.shutdown(wait=False, cancel_futures=True)
        error = f"{type(exc).__name__}: {exc}"
        return _error_result(
            task,
            error,
            failure=internal_error_failure(error),
            retry_count=retry_count,
        )


def _compose_task_prompt(task: dict[str, Any]) -> str:
    sections = []
    objective = str(task.get("objective") or "").strip()
    if objective:
        sections.append(f"Objective: {objective}")
    scope = task.get("scope")
    if scope:
        sections.append("Scope:\n" + json.dumps(scope, ensure_ascii=False, indent=2, default=str))
    budget = task.get("budget")
    if budget:
        sections.append("Budget:\n" + json.dumps(budget, ensure_ascii=False, indent=2, default=str))
    deliverable = str(task.get("deliverable") or "").strip()
    if deliverable:
        sections.append(f"Deliverable: {deliverable}")
    prompt = str(task.get("prompt") or "").strip()
    if prompt:
        sections.append(prompt)
    return "\n\n".join(sections)


def _subagent_span_id(parent_span_id: str | None, index: int) -> str | None:
    if not parent_span_id:
        return None
    return f"{parent_span_id}:subagent:{index}"


def _trace_auto_retry(
    *,
    trace_store,
    parent_run_state,
    parent_span_id: str | None,
    task_index: int,
    before: dict[str, Any],
    after: dict[str, Any],
) -> None:
    if trace_store is None or parent_run_state is None:
        return
    span_id = f"{_subagent_span_id(parent_span_id, task_index) or parent_span_id or 'subagent'}:retry:1"
    trace_store.append_event(
        parent_run_state,
        "subagent.retry.completed",
        {
            "task_index": task_index,
            "retry_count": int(after.get("retry_count") or 1),
            "initial_failure_reason": str(before.get("failure_reason") or ""),
            "final_failure_reason": str(after.get("failure_reason") or ""),
            "success": bool(after.get("success")),
            "recovered": bool(after.get("recovered")),
        },
        span_id=span_id,
        parent_span_id=_subagent_span_id(parent_span_id, task_index),
    )


def _checkpoint_parent_session(
    parent_session,
    tasks: list[dict[str, Any]],
    results: list[dict[str, Any]],
) -> None:
    if not WORKING_MEMORY_CHECKPOINT_ENABLED or parent_session is None:
        return
    checkpoint_subtask_results(parent_session, tasks, results)
