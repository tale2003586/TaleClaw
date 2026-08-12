"""Kernel-safe execution policy contracts and zero-side-effect defaults."""

from __future__ import annotations

from dataclasses import dataclass


class NoToolCallPolicy:
    def denial(self, session, tool_name: str, *, state=None) -> str:
        return ""

    def add_notice(self, session, tool_name: str, output: str, *, state=None) -> str:
        return output


class NoFinishingPolicy:
    def inject(self, session, reasoning_steps: int, **kwargs) -> None:
        return None

    def _reminder_step(self) -> int:
        return 2**31 - 1


@dataclass(frozen=True)
class ExecutionPolicies:
    tool_calls: object
    finishing: object

    @classmethod
    def minimal(cls, max_reasoning_steps: int = 1) -> "ExecutionPolicies":
        return cls(
            tool_calls=NoToolCallPolicy(),
            finishing=NoFinishingPolicy(),
        )


__all__ = ("ExecutionPolicies",)
