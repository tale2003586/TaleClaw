from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class TraceEvent:
    timestamp: str
    run_id: str
    event: str
    session_id: str = ""
    request_id: str = ""
    span_id: str | None = None
    parent_span_id: str | None = None
    step: int | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


RUN_STARTED = "run.started"
RUN_COMPLETED = "run.completed"
RUN_FAILED = "run.failed"
CONTEXT_BUILD_STARTED = "context.build.started"
CONTEXT_BUILD_COMPLETED = "context.build.completed"
CONTEXT_SANITIZED = "context.sanitized"
WORKSPACE_RESOLVED = "workspace.resolved"
WORKSPACE_SNAPSHOT_CAPTURED = "workspace.snapshot.captured"
WORKSPACE_DIFF_WRITTEN = "workspace.diff.written"
REASONING_STEP_STARTED = "reasoning.step.started"
REASONING_STEP_COMPLETED = "reasoning.step.completed"
MODEL_ROUTE_ATTEMPTS = "model.route.attempts"
MODEL_CALL_STARTED = "model.call.started"
MODEL_CALL_COMPLETED = "model.call.completed"
MODEL_CALL_FAILED = "model.call.failed"
TOOL_CALL_STARTED = "tool.call.started"
TOOL_CALL_COMPLETED = "tool.call.completed"
TOOL_CALL_FAILED = "tool.call.failed"
