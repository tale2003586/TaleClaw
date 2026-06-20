from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from datetime import datetime

from runtime.trace.events import RUN_STARTED, TraceEvent
from config import WORKDIR
from runtime.failure_reasons import INCOMPLETE_STEP_LIMIT_PREFIX
from runtime.trace.index_store import TraceIndexStore, trace_index_enabled
from runtime.trace.run_state import RunState, now_iso
from runtime.trace.summary import write_trace_summary


class TraceStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root) if root is not None else WORKDIR / ".runs"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.index_store = (
            TraceIndexStore(default_root=self.root)
            if trace_index_enabled()
            else None
        )

    def start_run(self, run_state: RunState) -> Path:
        run_dir = self.run_dir(run_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        self.write_run_state(run_state)
        self.append_event(run_state, RUN_STARTED, run_state.to_dict())
        self._index_run(run_state)
        return run_dir

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
            payload=_json_safe(payload or {}),
        ).to_dict()
        run_dir = self.run_dir(run_state)
        run_dir.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with (run_dir / "trace.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

    def write_run_state(self, run_state: RunState) -> None:
        if (run_state.metadata or {}).get("trace_only"):
            return
        self._write_json_atomic(
            self.run_dir(run_state) / "run_state.json",
            run_state.to_dict(),
        )

    def write_report(
        self,
        run_state: RunState,
        report: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "run_state": run_state.to_dict(),
            "report": _json_safe(report or {}),
            "generated_at": now_iso(),
        }
        self._write_json_atomic(self.run_dir(run_state) / "report.json", payload)
        self.write_metrics(run_state)
        write_trace_summary(self.run_dir(run_state))
        self._index_run_from_artifacts(run_state, report or {})

    def write_metrics(self, run_state: RunState) -> None:
        self._write_json_atomic(
            self.run_dir(run_state) / "metrics.json",
            self._metrics_for(run_state),
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
        self.index_store.upsert_run(run_state, run_dir=self.run_dir(run_state))

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
        self.index_store.upsert_run(
            run_state,
            run_dir=run_dir,
            report=report,
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
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "model_retry_count": 0,
            "model_route_attempts": 0,
            "sanitized_messages": 0,
            "duplicate_tool_call_ratio": 0.0,
            "duplicate_tool_call_count": 0,
            "truncated_tool_output_count": 0,
            "subagent_incomplete_count": 0,
            "subagent_fanout_count": 0,
            "run_duration_ms": _duration_ms(run_state.started_at, run_state.finished_at),
            "models": [],
            "tools": [],
            "generated_at": now_iso(),
        }
        trace_path = self.run_dir(run_state) / "trace.jsonl"
        if not trace_path.exists():
            return metrics

        models: set[str] = set()
        tools: set[str] = set()
        seen_tool_calls: set[str] = set()
        duplicate_tool_calls = 0
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
                metrics["input_tokens"] += _int(usage.get("input_tokens"))
                metrics["output_tokens"] += _int(usage.get("output_tokens"))
                metrics["total_tokens"] += _int(usage.get("total_tokens"))
                retry_count = _int(
                    (payload.get("provider_metadata") or {}).get("retry_count")
                )
                metrics["model_retry_count"] += retry_count
                model = str(payload.get("model") or "").strip()
                provider = str(payload.get("provider") or "").strip()
                if model or provider:
                    models.add(f"{provider}:{model}".strip(":"))
            elif name == "model.call.failed":
                metrics["model_failures"] += 1
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
                    metrics["subagent_incomplete_count"] += (
                        _int(payload.get("subagent_incomplete_count"))
                        or _subagent_incomplete_count(payload.get("output_preview"))
                    )
                fingerprint = _tool_call_fingerprint(payload)
                if fingerprint:
                    if fingerprint in seen_tool_calls:
                        duplicate_tool_calls += 1
                    else:
                        seen_tool_calls.add(fingerprint)
            elif name == "tool.call.failed":
                metrics["tool_calls"] += 1
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
        return metrics


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
    args = str(payload.get("final_arguments_preview") or "").strip()
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


def _subagent_incomplete_count(output_preview: Any) -> int:
    text = str(output_preview or "")
    if not text:
        return 0
    count = text.count('"truncated": true')
    count += text.count('"incomplete": true')
    count += text.count(INCOMPLETE_STEP_LIMIT_PREFIX)
    return count


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
