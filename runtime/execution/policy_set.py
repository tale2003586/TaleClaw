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


class NoWorkingMemoryPolicy:
    def partial_summary(self, session) -> str:
        lines = [
            "本轮已按用户请求停止。当前工具调用如果已经开始，会在完整结束后再停在这个边界。",
        ]
        for message in reversed(getattr(session, "messages", []) or []):
            if not isinstance(message, dict):
                continue
            if str(message.get("role") or "") not in {"assistant", "tool"}:
                continue
            content = str(message.get("content") or "").strip()
            if content:
                lines.extend(["", "最近可用进展：", content[:1200]])
                break
        else:
            lines.extend(["", "目前还没有可汇总的模型输出或工具结果。"])
        return "\n".join(lines)

    def checkpoint(self, session, profile, **payload) -> None:
        return None

    def complete(self, session, *, final_answer: str, step: int) -> None:
        return None

    def stop(self, session, profile, **payload) -> None:
        return None

    def enabled_for(self, profile) -> bool:
        return False


class SequentialToolBatchPolicy:
    def should_parallelize_tasks(self, tool_calls: list, *, available: bool) -> bool:
        return False

    def should_batch_reads(self, tool_calls: list, *, available: bool) -> bool:
        return False


@dataclass(frozen=True)
class ExecutionPolicies:
    web_search: object
    finishing: object
    working_memory: object
    tool_batch: object

    @classmethod
    def minimal(cls, max_reasoning_steps: int = 1) -> "ExecutionPolicies":
        return cls(
            web_search=NoWebSearchPolicy(),
            finishing=NoFinishingPolicy(),
            working_memory=NoWorkingMemoryPolicy(),
            tool_batch=SequentialToolBatchPolicy(),
        )


__all__ = ("ExecutionPolicies",)
