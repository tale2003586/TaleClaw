from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Callable
from datetime import datetime
from dataclasses import replace

from runtime.trace.events import RUN_STARTED, TraceEvent
from config import WORKDIR
from runtime.trace.context_metrics import (
    aggregate_context_metrics,
    context_metric_record_from_event,
)
from runtime.trace.index_store import TraceIndexStore, trace_index_enabled
from runtime.trace.run_state import RunState, now_iso
from runtime.trace.summary import write_trace_summary
from runtime.trace.trace_subscribers import TraceSubscribers


class TraceStore:
    def __init__(
        self,
        root: str | Path | None = None,
        *,
        index_enabled: bool | None = None,
        capture_content: bool = False,
    ) -> None:
        self.root = Path(root) if root is not None else WORKDIR / ".runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.capture_content = bool(capture_content)
        self._lock = threading.RLock()
        use_index = trace_index_enabled() if index_enabled is None else index_enabled
        self.index_store = (
            TraceIndexStore(default_root=self.root)
            if use_index
            else None
        )
        self._event_subscribers = TraceSubscribers(self._lock)

    def start_run(self, run_state: RunState) -> Path:
        run_dir = self.run_dir(run_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        if not (run_state.metadata or {}).get("trace_only"):
            self._event_subscribers.register_run(run_state.run_id, run_state.session_id)
        self.write_run_state(run_state)
        self.append_event(run_state, RUN_STARTED, run_state.to_dict())
        self._index_run(run_state)
        return run_dir

    def subscribe(
        self,
        session_id: str,
        cb: Callable[[dict[str, Any]], None],
    ) -> Callable[[], None]:
        return self._event_subscribers.subscribe(session_id, cb)

    def append_event(
        self,
        run_state: RunState,
        event_name: str,
        payload: dict[str, Any] | None = None,
        *,
        span_id: str | None = None,
        parent_span_id: str | None = None,
        request_id: str | None = None,
        session_id: str | None = None,
        step: int | None = None,
    ) -> None:
        event = TraceEvent(
            timestamp=now_iso(),
            run_id=run_state.run_id,
            session_id=session_id or run_state.session_id,
            request_id=request_id or _request_id_for(run_state),
            event=event_name,
            span_id=span_id,
            parent_span_id=parent_span_id,
            step=step,
            payload=self._trace_payload(payload or {}),
        ).to_dict()
        run_dir = self.run_dir(run_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
            subscribers = self._event_subscribers.snapshot(
                run_state.run_id,
                run_state.session_id,
            )
        self._event_subscribers.dispatch(event, subscribers)

    def _trace_payload(self, payload):
        value = _json_safe(payload)
        return value if self.capture_content else _metadata_only(value)

    def write_run_state(self, run_state: RunState) -> None:
        if (run_state.metadata or {}).get("trace_only"):
            return
        self._write_json_atomic(
            self.run_dir(run_state) / "run_state.json",
            self._trace_payload(run_state.to_dict()),
        )

    def write_report(
        self,
        run_state: RunState,
        report: dict[str, Any] | None = None,
    ) -> None:
        try:
            payload = {
                "run_state": self._trace_payload(run_state.to_dict()),
                "report": self._trace_payload(report or {}),
                "generated_at": now_iso(),
            }
            self._write_json_atomic(self.run_dir(run_state) / "report.json", payload)
            self.write_metrics(run_state)
            self.write_context_metrics(run_state)
            write_trace_summary(self.run_dir(run_state))
            self._index_run_from_artifacts(run_state, report or {})
        finally:
            if not (run_state.metadata or {}).get("trace_only"):
                self._event_subscribers.clear_run(run_state.run_id)

    def write_metrics(self, run_state: RunState) -> None:
        self._write_json_atomic(
            self.run_dir(run_state) / "metrics.json",
            self._metrics_for(run_state),
        )

    def write_context_metrics(self, run_state: RunState) -> None:
        self._write_json_atomic(
            self.run_dir(run_state) / "context_metrics.json",
            self._context_metrics_for(run_state),
        )

    def run_dir(self, run_state_or_id: RunState | str) -> Path:
        run_id = (
            run_state_or_id.run_id
            if isinstance(run_state_or_id, RunState)
            else str(run_state_or_id)
        )
        return self.root / run_id

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(
                json.dumps(_json_safe(payload), indent=2, ensure_ascii=False, default=str)
                + "\n",
                encoding="utf-8",
            )
            os.replace(tmp_path, path)

    def _index_run(self, run_state: RunState) -> None:
        if self.index_store is None:
            return
        indexed = run_state if self.capture_content else replace(run_state, final_answer=None, error=None, metadata=self._trace_payload(run_state.metadata))
        self.index_store.upsert_run(indexed, run_dir=self.run_dir(run_state))

    def _index_run_from_artifacts(
        self,
        run_state: RunState,
        report: dict[str, Any],
    ) -> None:
        if self.index_store is None:
            return
        run_dir = self.run_dir(run_state)
        metrics = _read_json(run_dir / "metrics.json")
        summary = _read_json(run_dir / "trace_summary.json")
        indexed = run_state if self.capture_content else replace(run_state, final_answer=None, error=None, metadata=self._trace_payload(run_state.metadata))
        self.index_store.upsert_run(
            indexed,
            run_dir=run_dir,
            report=self._trace_payload(report),
            summary=summary,
            metrics=metrics,
        )
        execution_path = summary.get("execution_path")
        if isinstance(execution_path, list):
            self.index_store.replace_steps(run_state.run_id, execution_path)

    def _metrics_for(self, run_state: RunState) -> dict[str, Any]:
        metrics = {
            "run_id": run_state.run_id,
            "session_id": run_state.session_id,
            "status": run_state.status,
            "reasoning_steps": run_state.reasoning_steps,
            "model_calls": 0,
            "model_failures": 0,
            "tool_calls": 0,
            "tool_failures": 0,
            "tool_denials": 0,
            "total_model_duration_ms": 0.0,
            "total_tool_duration_ms": 0.0,
            "unknown_usage_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            # Sum only reported cache reads; None means no reported counter.
            # The subtotal remains partial when unknown_cache_usage_calls > 0.
            "cached_input_tokens": None,
            "unknown_cache_usage_calls": 0,
            "cache_usage_scope": "trace_main_calls",
            "model_retry_count": 0,
            "model_route_attempts": 0,
            "sanitized_messages": 0,
            "duplicate_tool_call_ratio": 0.0,
            "duplicate_tool_call_count": 0,
            "truncated_tool_output_count": 0,
            "subagent_fanout_count": 0,
            "run_duration_ms": _duration_ms(run_state.started_at, run_state.finished_at),
            "models": [],
            "tools": [],
            "generated_at": now_iso(),
        }
        trace_path = self.run_dir(run_state) / "trace.jsonl"

        models: set[str] = set()
        tools: set[str] = set()
        seen_tool_calls: set[str] = set()
        duplicate_tool_calls = 0
        if trace_path.exists():
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                name = event.get("event")
                payload = event.get("payload") or {}
                if name == "model.call.completed":
                    metrics["model_calls"] += 1
                    metrics["total_model_duration_ms"] += _float(payload.get("duration_ms"))
                    usage = payload.get("usage") or {}
                    if not all(isinstance(usage.get(key), int) and not isinstance(usage.get(key), bool) for key in ("input_tokens", "output_tokens", "total_tokens")):
                        metrics["unknown_usage_calls"] += 1
                    metrics["input_tokens"] += _int(usage.get("input_tokens"))
                    metrics["output_tokens"] += _int(usage.get("output_tokens"))
                    metrics["total_tokens"] += _int(usage.get("total_tokens"))
                    cached = usage.get("cached_input_tokens")
                    input_tokens = usage.get("input_tokens")
                    total_tokens = usage.get("total_tokens")
                    if (
                        isinstance(cached, int) and not isinstance(cached, bool) and cached >= 0
                        and not (isinstance(input_tokens, int) and not isinstance(input_tokens, bool)
                                 and cached > input_tokens)
                        and not (isinstance(total_tokens, int) and not isinstance(total_tokens, bool)
                                 and cached > total_tokens)
                    ):
                        metrics["cached_input_tokens"] = (metrics["cached_input_tokens"] or 0) + cached
                    else:
                        metrics["unknown_cache_usage_calls"] += 1
                    retry_count = _int(
                        (payload.get("provider_metadata") or {}).get("retry_count")
                    )
                    metrics["model_retry_count"] += retry_count
                    # Failed fallback attempts have no cache breakdown in this event.
                    metrics["unknown_cache_usage_calls"] += max(0, retry_count)
                    model = str(payload.get("model") or "").strip()
                    provider = str(payload.get("provider") or "").strip()
                    if model or provider:
                        models.add(f"{provider}:{model}".strip(":"))
                elif name == "model.call.failed":
                    metrics["model_failures"] += 1
                    metrics["unknown_usage_calls"] += max(1, len(payload.get("route_attempts") or []))
                    metrics["unknown_cache_usage_calls"] += max(1, len(payload.get("route_attempts") or []))
                    metrics["total_model_duration_ms"] += _float(payload.get("duration_ms"))
                    metrics["model_route_attempts"] += len(payload.get("route_attempts") or [])
                elif name == "model.route.attempts":
                    if not metrics["model_route_attempts"]:
                        metrics["model_route_attempts"] = len(payload.get("attempts") or [])
                elif name == "context.sanitized":
                    metrics["sanitized_messages"] += _int(payload.get("dropped_count"))
                elif name == "tool.call.completed":
                    metrics["tool_calls"] += 1
                    metrics["total_tool_duration_ms"] += _float(payload.get("duration_ms"))
                    status = str(payload.get("status") or "")
                    if status == "error":
                        metrics["tool_failures"] += 1
                    elif status == "denied":
                        metrics["tool_denials"] += 1
                    tool_name = str(payload.get("tool_name") or "").strip()
                    if tool_name:
                        tools.add(tool_name)
                    if payload.get("truncated_output"):
                        metrics["truncated_tool_output_count"] += 1
                    if tool_name == "parallel_tasks":
                        metrics["subagent_fanout_count"] += 1
                    fingerprint = _tool_call_fingerprint(payload)
                    if fingerprint:
                        if fingerprint in seen_tool_calls:
                            duplicate_tool_calls += 1
                        else:
                            seen_tool_calls.add(fingerprint)
                elif name == "tool.call.failed":
                    metrics["tool_calls"] += 1
                    if payload.get("status") == "denied":
                        metrics["tool_denials"] += 1
                    else:
                        metrics["tool_failures"] += 1
                    metrics["total_tool_duration_ms"] += _float(payload.get("duration_ms"))
                    tool_name = str(payload.get("tool_name") or "").strip()
                    if tool_name:
                        tools.add(tool_name)
                    fingerprint = _tool_call_fingerprint(payload)
                    if fingerprint:
                        if fingerprint in seen_tool_calls:
                            duplicate_tool_calls += 1
                        else:
                            seen_tool_calls.add(fingerprint)

        # Root counters include auxiliary purposes and every fallback attempt.
        root_budget = (run_state.metadata or {}).get("execution_budget")
        if isinstance(root_budget, dict) and _int(root_budget.get("model_attempts")) > 0 and _int(root_budget.get("model_attempts")) >= metrics["model_calls"] + metrics["model_failures"]:
            metrics["root_model_attempts"] = root_budget.get("model_attempts")
            metrics["root_tool_attempts"] = root_budget.get("tool_attempts")
            metrics["total_tokens"] = _int(root_budget.get("known_tokens"))
            metrics["unknown_usage_calls"] = _int(root_budget.get("unknown_usage_attempts")) + _int(root_budget.get("model_attempts_in_flight"))
            metrics["model_attempts_in_flight"] = _int(root_budget.get("model_attempts_in_flight"))
            metrics["usage_scope"] = "root_all_purposes"
            # Breakdown is only available for main-loop calls, never label it root total.
            metrics["input_tokens"] = None
            metrics["output_tokens"] = None
        else:
            metrics["usage_scope"] = "trace_main_calls"
        for key in ("input_tokens", "output_tokens", "total_tokens"):
            metrics[f"known_{key}"] = metrics[key]
            if metrics["unknown_usage_calls"]:
                metrics[key] = None
        metrics["usage_status"] = ("complete" if not metrics["unknown_usage_calls"]
            else "partial" if metrics["known_total_tokens"] else "unknown")
        context_metrics = self._context_metrics_for(run_state)
        context_aggregate = context_metrics.get("aggregate") or {}
        metrics["total_model_duration_ms"] = round(metrics["total_model_duration_ms"], 3)
        metrics["total_tool_duration_ms"] = round(metrics["total_tool_duration_ms"], 3)
        metrics["duplicate_tool_call_count"] = duplicate_tool_calls
        metrics["duplicate_tool_call_ratio"] = (
            round(duplicate_tool_calls / metrics["tool_calls"], 4)
            if metrics["tool_calls"]
            else 0.0
        )
        metrics["models"] = sorted(models)
        metrics["tools"] = sorted(tools)
        metrics["context_builds"] = _int(context_aggregate.get("build_count"))
        metrics["context_build_compressed_count"] = _int(
            context_aggregate.get("compressed_build_count")
        )
        metrics["total_context_build_duration_ms"] = _float(
            context_aggregate.get("total_build_duration_ms")
        )
        metrics["avg_context_build_duration_ms"] = _float(
            context_aggregate.get("avg_build_duration_ms")
        )
        metrics["max_context_build_duration_ms"] = _float(
            context_aggregate.get("max_build_duration_ms")
        )
        metrics["context_compression_before_chars"] = _int(
            context_aggregate.get("compression_before_chars")
        )
        metrics["context_compression_after_chars"] = _int(
            context_aggregate.get("compression_after_chars")
        )
        metrics["context_compression_saved_chars"] = _int(
            context_aggregate.get("compression_saved_chars")
        )
        metrics["context_compression_ratio"] = _float(
            context_aggregate.get("compression_ratio")
        )
        metrics["context_compression_savings_ratio"] = _float(
            context_aggregate.get("compression_savings_ratio")
        )
        metrics["context_token_compression_before_tokens"] = _int(
            context_aggregate.get("compression_before_tokens")
        )
        metrics["context_token_compression_after_tokens"] = _int(
            context_aggregate.get("compression_after_tokens")
        )
        metrics["context_token_compression_saved_tokens"] = _int(
            context_aggregate.get("compression_saved_tokens")
        )
        metrics["context_token_compression_ratio"] = _float(
            context_aggregate.get("token_compression_ratio")
        )
        metrics["context_token_compression_savings_ratio"] = _float(
            context_aggregate.get("token_compression_savings_ratio")
        )
        metrics["coding_context_builds"] = _int(
            context_aggregate.get("coding_context_build_count")
        )
        metrics["coding_context_compacted_count"] = _int(
            context_aggregate.get("coding_context_compacted_count")
        )
        metrics["coding_context_latest_generation"] = _int(
            context_aggregate.get("coding_context_latest_generation")
        )
        metrics["coding_context_max_generation"] = _int(
            context_aggregate.get("coding_context_max_generation")
        )
        metrics["coding_context_latest_prompt_tail_start_index"] = _int(
            context_aggregate.get("coding_context_latest_prompt_tail_start_index")
        )
        metrics["coding_context_latest_compacted_until_index"] = _int(
            context_aggregate.get("coding_context_latest_compacted_until_index")
        )
        return metrics

    def _context_metrics_for(self, run_state: RunState) -> dict[str, Any]:
        trace_path = self.run_dir(run_state) / "trace.jsonl"
        records: list[dict[str, Any]] = []
        if trace_path.exists():
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get("event") != "context.build.completed":
                    continue
                records.append(context_metric_record_from_event(event))
        return {
            "run_id": run_state.run_id,
            "session_id": run_state.session_id,
            "status": run_state.status,
            "generated_at": now_iso(),
            "aggregate": aggregate_context_metrics(records),
            "builds": records,
        }


def event_preview(value: Any, *, limit: int = 500) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _tool_call_fingerprint(payload: dict[str, Any]) -> str:
    tool_name = str(payload.get("tool_name") or "").strip()
    args = str(payload.get("final_arguments_preview_sha256") or payload.get("final_arguments_preview") or "").strip()
    if not tool_name:
        return ""
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": args,
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def _request_id_for(run_state: RunState) -> str:
    metadata = run_state.metadata or {}
    return str(metadata.get("request_id") or run_state.run_id)


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _duration_ms(started_at: str | None, finished_at: str | None) -> float | None:
    if not started_at or not finished_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        finished = datetime.fromisoformat(finished_at)
    except ValueError:
        return None
    return round((finished - started).total_seconds() * 1000, 3)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _json_safe(item)
            for key, item in value.items()
            if not _looks_secret(str(key))
        }
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return sorted(_json_safe(item) for item in value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "__dict__"):
        return _json_safe(value.__dict__)
    return str(value)


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in {"token", "password", "secret", "api_key", "apikey"}
        or lowered.endswith("_token")
        or lowered.endswith("-token")
        or any(
            marker in lowered
            for marker in (
                "api_key",
                "apikey",
                "authorization",
                "access_token",
                "refresh_token",
            )
        )
    )


# Default persistence contains bounded operational metadata, not arbitrary text.
# Full diagnostic content is explicit opt-in via TraceStore(capture_content=True).
_TRACE_STRING_FIELDS = frozenset({
    "event", "event_id", "schema_version", "timestamp", "run_id", "session_id", "request_id",
    "span_id", "parent_span_id", "task_id", "parent_task_id", "model", "provider", "tool_name", "tool_mode", "status",
    "execution_status", "task_status", "outcome", "effect_state", "error_type", "stop_reason",
    "started_at", "finished_at", "generated_at", "active_agent", "agent_name", "agent_type",
    "route_purpose", "selected_profile", "selected_provider", "selected_model", "profile",
    "purpose", "usage_status", "phase", "source", "kind", "condition", "decision",
    "cache_status", "policy", "counter_kind", "budget_kind", "verifier", "patch_sha256",
    "section", "strategy", "mode", "agent", "channel", "execution_path", "last_tool",
    "label", "fallback_reason", "prompt_version", "usage_scope",
})


def _metadata_only(value, key=""):
    if isinstance(value, dict):
        out = {}
        for name, item in value.items():
            if name in {"arguments", "final_arguments", "arguments_preview", "final_arguments_preview"}:
                # Group equal calls without retaining their argument content.
                import hashlib
                out[name + "_sha256"] = hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()
            elif isinstance(item, str) and name not in _TRACE_STRING_FIELDS:
                out[name + "_chars"] = len(item)
            elif name in {"messages", "content", "reply", "prompt", "query", "input_text", "output", "raw_response", "text"}:
                out[name + "_chars"] = len(str(item))
            else:
                out[name] = _metadata_only(item, name)
        return out
    if isinstance(value, list):
        return [_metadata_only(item, key) for item in value]
    if isinstance(value, str):
        return value if key in _TRACE_STRING_FIELDS else {"chars": len(value)}
    return value
