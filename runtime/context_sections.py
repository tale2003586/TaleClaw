from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any


@dataclass
class ContextSection:
    name: str
    raw_chars: int
    rendered_chars: int
    budget_chars: int | None = None
    truncated: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_text(
        cls,
        name: str,
        text: str,
        *,
        raw_text: str | None = None,
        budget_chars: int | None = None,
        truncated: bool = False,
        metadata: dict[str, Any] | None = None,
    ) -> "ContextSection":
        raw = text if raw_text is None else raw_text
        return cls(
            name=name,
            raw_chars=len(str(raw or "")),
            rendered_chars=len(str(text or "")),
            budget_chars=budget_chars,
            truncated=bool(truncated),
            metadata=dict(metadata or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "raw_chars": self.raw_chars,
            "rendered_chars": self.rendered_chars,
            "budget_chars": self.budget_chars,
            "truncated": self.truncated,
            "metadata": self.metadata,
        }


@dataclass
class ContextBuildReport:
    total_chars: int
    budget_chars: int | None = None
    over_budget: bool = False
    sections: list[ContextSection] = field(default_factory=list)
    reductions: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "total_chars": self.total_chars,
            "budget_chars": self.budget_chars,
            "over_budget": self.over_budget,
            "sections": {
                section.name: section.to_dict()
                for section in self.sections
            },
            "reductions": list(self.reductions),
            "metadata": dict(self.metadata),
        }


def message_chars(messages: list[dict]) -> int:
    total = 0
    for message in messages or []:
        if not isinstance(message, dict):
            total += len(str(message))
            continue
        total += len(str(message.get("role") or ""))
        content = message.get("content")
        total += len(str(content or ""))
        for key in ("tool_calls", "tool_call_id", "name", "status"):
            if key in message:
                total += len(json.dumps(message.get(key), ensure_ascii=False, default=str))
    return total
