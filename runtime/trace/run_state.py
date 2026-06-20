from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def new_run_id(prefix: str = "run") -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{prefix}_{stamp}-{uuid4().hex[:6]}"


@dataclass
class RunState:
    run_id: str
    session_id: str
    channel: str = ""
    chat_id: str = ""
    user_id: str | None = None
    user_role: str | None = None
    mode: str = ""
    execution_path: str = ""
    status: str = "running"
    reasoning_steps: int = 0
    tool_calls: int = 0
    last_tool: str | None = None
    stop_reason: str | None = None
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    final_answer: str | None = None
    error: str | None = None
    intent: str = ""
    profile: str = ""
    metadata: dict = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        channel: str = "",
        chat_id: str = "",
        user_id: str | None = None,
        user_role: str | None = None,
        mode: str = "",
        execution_path: str = "",
        intent: str = "",
        profile: str = "",
        metadata: dict | None = None,
        run_id: str | None = None,
    ) -> "RunState":
        return cls(
            run_id=run_id or new_run_id(),
            session_id=session_id,
            channel=channel,
            chat_id=chat_id,
            user_id=user_id,
            user_role=user_role,
            mode=mode,
            execution_path=execution_path,
            intent=intent,
            profile=profile,
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict:
        return asdict(self)

    def set_route(
        self,
        *,
        mode: str,
        execution_path: str,
        intent: str = "",
        profile: str = "",
    ) -> None:
        self.mode = mode
        self.execution_path = execution_path
        self.intent = intent
        self.profile = profile

    def record_reasoning_step(self, step: int | None = None) -> None:
        if step is None:
            self.reasoning_steps += 1
        else:
            self.reasoning_steps = max(self.reasoning_steps, int(step))

    def record_tool(self, name: str) -> None:
        self.tool_calls += 1
        self.last_tool = name

    def finish(
        self,
        final_answer: str | None = None,
        *,
        status: str = "completed",
        stop_reason: str | None = None,
    ) -> None:
        self.status = status
        self.stop_reason = stop_reason
        self.final_answer = final_answer
        self.finished_at = now_iso()

    def finish_success(self, final_answer: str | None = None) -> None:
        self.finish(final_answer, status="completed")

    def stop(self, reason: str, final_answer: str | None = None) -> None:
        self.finish(final_answer, status="stopped", stop_reason=reason)

    def fail(self, error: BaseException | str) -> None:
        self.status = "failed"
        self.error = (
            f"{type(error).__name__}: {error}"
            if isinstance(error, BaseException)
            else str(error)
        )
        self.finished_at = now_iso()
