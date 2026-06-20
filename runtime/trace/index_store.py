from __future__ import annotations

import json
import os
import threading
from contextlib import closing
from pathlib import Path
from typing import Any

from runtime.db import connect, execute_many, resolve_database_config, sql
from runtime.trace.run_state import RunState, now_iso


class TraceIndexStore:
    """Lightweight relational index for file-backed trace artifacts."""

    def __init__(self, database_url: str | Path | None = None, *, default_root: Path | None = None) -> None:
        self.config = resolve_database_config(
            database_url,
            env_names=("TRACE_DATABASE_URL", "DATABASE_URL"),
            purpose="trace index store",
        )
        self._lock = threading.Lock()
        self._init_schema()

    def _connect(self):
        return connect(self.config)

    def _init_schema(self) -> None:
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS trace_runs (
                    run_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    channel TEXT,
                    chat_id TEXT,
                    user_id TEXT,
                    user_role TEXT,
                    mode TEXT,
                    execution_path TEXT,
                    status TEXT,
                    stop_reason TEXT,
                    failure_category TEXT,
                    failure_reason TEXT,
                    workspace_root TEXT,
                    workspace_requested TEXT,
                    workspace_allowed_root TEXT,
                    trace_path TEXT NOT NULL,
                    report_path TEXT,
                    summary_path TEXT,
                    metrics_path TEXT,
                    reasoning_steps INTEGER NOT NULL DEFAULT 0,
                    model_calls_count INTEGER NOT NULL DEFAULT 0,
                    tool_calls_count INTEGER NOT NULL DEFAULT 0,
                    tool_failures_count INTEGER NOT NULL DEFAULT 0,
                    tool_denials_count INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    duration_ms REAL,
                    last_tool TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE TABLE IF NOT EXISTS trace_steps (
                    id BIGSERIAL PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    reasoning_step INTEGER,
                    label TEXT NOT NULL,
                    status TEXT,
                    detail TEXT,
                    duration_ms REAL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES trace_runs(run_id) ON DELETE CASCADE
                )
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_trace_runs_started
                ON trace_runs(started_at DESC)
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_trace_runs_status
                ON trace_runs(status, failure_category)
                """)
            )
            conn.execute(
                sql(self.config, """
                CREATE INDEX IF NOT EXISTS idx_trace_steps_run
                ON trace_steps(run_id, step_index)
                """)
            )
            conn.commit()

    def upsert_run(
        self,
        run_state: RunState,
        *,
        run_dir: Path,
        report: dict[str, Any] | None = None,
        summary: dict[str, Any] | None = None,
        metrics: dict[str, Any] | None = None,
    ) -> None:
        report = report or {}
        summary = summary or {}
        metrics = metrics or {}
        failure = summary.get("failure") or {}
        workspace = summary.get("workspace") or {}
        summary_metrics = summary.get("metrics") or {}
        now = now_iso()
        payload = (
            run_state.run_id,
            run_state.session_id,
            run_state.channel,
            run_state.chat_id,
            run_state.user_id,
            run_state.user_role,
            run_state.mode,
            run_state.execution_path,
            run_state.status,
            run_state.stop_reason,
            failure.get("category", ""),
            failure.get("reason", ""),
            workspace.get("resolved") or report.get("workspace_root") or "",
            workspace.get("requested") or report.get("workspace_root") or "",
            workspace.get("allowed_root") or "",
            str(run_dir / "trace.jsonl"),
            str(run_dir / "report.json"),
            str(run_dir / "trace_summary.json"),
            str(run_dir / "metrics.json"),
            int(run_state.reasoning_steps or 0),
            _int(metrics.get("model_calls", summary_metrics.get("model_calls"))),
            _int(metrics.get("tool_calls", summary_metrics.get("tool_calls", run_state.tool_calls))),
            _int(metrics.get("tool_failures", summary_metrics.get("tool_failures"))),
            _int(metrics.get("tool_denials", summary_metrics.get("tool_denials"))),
            _int(metrics.get("total_tokens", summary_metrics.get("total_tokens"))),
            _float(metrics.get("run_duration_ms", summary_metrics.get("duration_ms"))),
            run_state.last_tool,
            run_state.started_at,
            run_state.finished_at,
            json.dumps(run_state.metadata or {}, ensure_ascii=False, sort_keys=True, default=str),
            run_state.started_at or now,
            now,
        )
        with self._lock, closing(self._connect()) as conn:
            conn.execute(
                sql(self.config, """
                INSERT INTO trace_runs (
                    run_id, session_id, channel, chat_id, user_id, user_role, mode,
                    execution_path, status, stop_reason, failure_category, failure_reason,
                    workspace_root, workspace_requested, workspace_allowed_root,
                    trace_path, report_path, summary_path, metrics_path,
                    reasoning_steps, model_calls_count, tool_calls_count,
                    tool_failures_count, tool_denials_count, total_tokens, duration_ms,
                    last_tool, started_at, finished_at, metadata, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    session_id = excluded.session_id,
                    channel = excluded.channel,
                    chat_id = excluded.chat_id,
                    user_id = excluded.user_id,
                    user_role = excluded.user_role,
                    mode = excluded.mode,
                    execution_path = excluded.execution_path,
                    status = excluded.status,
                    stop_reason = excluded.stop_reason,
                    failure_category = excluded.failure_category,
                    failure_reason = excluded.failure_reason,
                    workspace_root = excluded.workspace_root,
                    workspace_requested = excluded.workspace_requested,
                    workspace_allowed_root = excluded.workspace_allowed_root,
                    trace_path = excluded.trace_path,
                    report_path = excluded.report_path,
                    summary_path = excluded.summary_path,
                    metrics_path = excluded.metrics_path,
                    reasoning_steps = excluded.reasoning_steps,
                    model_calls_count = excluded.model_calls_count,
                    tool_calls_count = excluded.tool_calls_count,
                    tool_failures_count = excluded.tool_failures_count,
                    tool_denials_count = excluded.tool_denials_count,
                    total_tokens = excluded.total_tokens,
                    duration_ms = excluded.duration_ms,
                    last_tool = excluded.last_tool,
                    started_at = excluded.started_at,
                    finished_at = excluded.finished_at,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """),
                payload,
            )
            conn.commit()

    def replace_steps(self, run_id: str, execution_path: list[dict[str, Any]]) -> None:
        now = now_iso()
        rows = [
            (
                run_id,
                index,
                str(item.get("kind") or ""),
                _optional_int(item.get("step")),
                str(item.get("label") or ""),
                str(item.get("status") or ""),
                str(item.get("detail") or ""),
                _float(item.get("duration_ms")),
                now,
            )
            for index, item in enumerate(execution_path)
        ]
        with self._lock, closing(self._connect()) as conn:
            conn.execute(sql(self.config, "DELETE FROM trace_steps WHERE run_id = ?"), (run_id,))
            if rows:
                execute_many(
                    conn,
                    self.config,
                    """
                    INSERT INTO trace_steps (
                        run_id, step_index, kind, reasoning_step, label,
                        status, detail, duration_ms, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    rows,
                )
            conn.commit()


def trace_index_enabled() -> bool:
    return str(os.getenv("TRACE_INDEX_ENABLED", "1")).strip().lower() not in {"0", "false", "no", "off"}


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _optional_int(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
