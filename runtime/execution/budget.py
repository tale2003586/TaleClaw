"""One cooperative, thread-safe resource budget for an entire inbound execution.

This is an in-process stop boundary, not a claim of remote request cancellation.
Known usage and missing usage are tracked separately; no money estimate is inferred.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
from uuid import uuid4
from threading import Lock
from time import monotonic
from typing import Callable


class ExecutionCancelled(RuntimeError):
    pass


class ExecutionBudgetExceeded(RuntimeError):
    pass


class ExecutionEffectUncertain(RuntimeError):
    pass


@dataclass
class ExecutionBudget:
    cancel_requested: Callable[[], bool] | None = None
    deadline: float = field(default_factory=lambda: monotonic() + 900)
    max_model_attempts: int = 64
    max_tool_attempts: int = 128
    max_known_tokens: int = 500_000
    model_attempts: int = 0
    tool_attempts: int = 0
    known_tokens: int = 0
    unknown_usage_attempts: int = 0
    known_usage_attempts: int = 0
    usage_totals: dict[str, int] = field(default_factory=dict)
    usage_field_counts: dict[str, int] = field(default_factory=dict)
    _request_identity: str = field(default_factory=lambda: uuid4().hex, repr=False)
    closed: bool = False
    stop_message: str = ""
    _lock: Lock = field(default_factory=Lock, repr=False)

    def check(self) -> None:
        if self.closed:
            raise ExecutionCancelled("Root execution scope is closed.")
        if self.stop_message:
            raise ExecutionBudgetExceeded(self.stop_message)
        if self.cancel_requested is not None:
            try:
                cancelled = self.cancel_requested()
            except Exception as exc:
                raise ExecutionCancelled("Cancellation status unavailable; stopping safely.") from exc
            if cancelled:
                raise ExecutionCancelled("Execution cancelled.")
        self._check_resource_limits()

    def _check_resource_limits(self) -> None:
        if monotonic() >= self.deadline:
            raise ExecutionBudgetExceeded("Root execution deadline exceeded.")
        if self.known_tokens >= self.max_known_tokens:
            raise ExecutionBudgetExceeded("Known token budget exhausted.")

    def stop(self, message: str) -> None:
        with self._lock:
            self.stop_message = message or "Root execution stopped."

    def close(self) -> None:
        with self._lock:
            self.closed = True

    def claim(self, kind: str) -> None:
        self.check()
        with self._lock:
            if self.closed:
                raise ExecutionCancelled("Root execution scope is closed.")
            if self.stop_message:
                raise ExecutionBudgetExceeded(self.stop_message)
            # Usage can settle, or the deadline can expire, after the unlocked
            # preflight. Recheck under the same lock that grants the attempt.
            self._check_resource_limits()
            if kind == "model":
                if self.model_attempts >= self.max_model_attempts:
                    raise ExecutionBudgetExceeded("Root model attempt budget exhausted.")
                self.model_attempts += 1
            elif kind == "tool":
                if self.tool_attempts >= self.max_tool_attempts:
                    raise ExecutionBudgetExceeded("Root tool attempt budget exhausted.")
                self.tool_attempts += 1
            else:
                raise ValueError(f"Unknown budget operation: {kind}")

    def record_usage(self, usage) -> None:
        value = getattr(usage, "total_tokens", None)
        if value is None and usage is not None:
            values = (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))
            if all(isinstance(v, int) and not isinstance(v, bool) and v >= 0 for v in values):
                value = sum(values)
        with self._lock:
            for name in ("input_tokens", "output_tokens", "cached_input_tokens"):
                amount = getattr(usage, name, None)
                if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                    self.usage_totals[name] = self.usage_totals.get(name, 0) + amount
                    self.usage_field_counts[name] = self.usage_field_counts.get(name, 0) + 1
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                self.known_tokens += value
                self.known_usage_attempts += 1
            else:
                self.unknown_usage_attempts += 1

    def snapshot(self) -> dict:
        with self._lock:
            return {"model_attempts": self.model_attempts, "tool_attempts": self.tool_attempts,
                    "known_tokens": self.known_tokens, "unknown_usage_attempts": self.unknown_usage_attempts,
                    "known_usage_attempts": self.known_usage_attempts,
                    "known_usage_totals": dict(self.usage_totals),
                    "usage_field_counts": dict(self.usage_field_counts),
                    "model_attempts_in_flight": max(0, self.model_attempts - self.known_usage_attempts - self.unknown_usage_attempts),
                    "closed": self.closed,
                    "usage_complete": (self.unknown_usage_attempts == 0 and self.model_attempts <= self.known_usage_attempts),
                    "remaining_seconds": max(0.0, self.deadline - monotonic())}


_CURRENT: ContextVar[ExecutionBudget | None] = ContextVar("execution_budget", default=None)
_SESSION: ContextVar[str | None] = ContextVar("execution_session", default=None)


def current_budget() -> ExecutionBudget | None:
    return _CURRENT.get()


def model_request_session_id() -> str:
    """Opaque routing identity; never send internal user/session names upstream."""
    identity = _SESSION.get()
    if not identity:
        budget = current_budget()
        identity = budget._request_identity if budget is not None else uuid4().hex
    return "taleclaw-" + hashlib.sha256(("TaleClaw:model-session:" + identity).encode()).hexdigest()


@contextmanager
def execution_scope(*, budget: ExecutionBudget | None = None, cancel_requested=None, session_id: str | None = None):
    # Conversation identity may change inside one root budget (coding tasks and
    # subagents). Context-local binding keeps pooled clients concurrency-safe.
    session_token = _SESSION.set(str(session_id)) if session_id else None
    existing = current_budget()
    try:
        if existing is not None:
            yield existing
            return
        active = budget or ExecutionBudget(cancel_requested=cancel_requested)
        token = _CURRENT.set(active)
        try:
            # Callers map cancellation to their public outcome; do not spend work here.
            yield active
        finally:
            active.close()
            _CURRENT.reset(token)
    finally:
        if session_token is not None:
            _SESSION.reset(session_token)
