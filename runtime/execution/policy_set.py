"""Kernel-safe execution policy contracts and zero-side-effect defaults."""

from __future__ import annotations

from dataclasses import dataclass


class NoWebSearchPolicy:
    def denial(self, session, tool_name: str, *, state=None) -> str:
        return ""

    def add_notice(self, session, tool_name: str, output: str, *, state=None) -> str:
        return output


class NoFinishingPolicy:
    def inject(self, session, reasoning_steps: int, **kwargs) -> None:
        return None

    def _reminder_step(self) -> int:
        return 2**31 - 1


class SequentialToolBatchPolicy:
    def should_parallelize_tasks(self, tool_calls: list, *, available: bool) -> bool:
        return False

    def should_batch_reads(self, tool_calls: list, *, available: bool) -> bool:
        return False


@dataclass(frozen=True)
class ExecutionPolicies:
    web_search: object
    finishing: object
    tool_batch: object

    @classmethod
    def minimal(cls, max_reasoning_steps: int = 1) -> "ExecutionPolicies":
        return cls(
            web_search=NoWebSearchPolicy(),
            finishing=NoFinishingPolicy(),
            tool_batch=SequentialToolBatchPolicy(),
        )


__all__ = ("ExecutionPolicies",)
