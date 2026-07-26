"""Explicit identity for a child Runtime invocation."""

from __future__ import annotations

from dataclasses import dataclass
import uuid

from runtime.runtime import RunExecutionState


@dataclass(frozen=True)
class ChildRun:
    run_id: str
    parent_run_id: str

    @classmethod
    def create(cls, parent_run_state=None) -> "ChildRun":
        return cls(
            run_id=f"child_{uuid.uuid4().hex}",
            parent_run_id=str(
                getattr(parent_run_state, "run_id", "") or ""
            ),
        )

    def execution_state(self) -> RunExecutionState:
        return RunExecutionState(
            run_id=self.run_id,
            parent_run_id=self.parent_run_id,
        )
