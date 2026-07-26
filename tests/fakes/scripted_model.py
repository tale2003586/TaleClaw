from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from models.provider import LLMResponse, LLMUsage, ToolCall


@dataclass(frozen=True)
class FinalResponse:
    content: str
    usage: LLMUsage | None = None


@dataclass(frozen=True)
class ToolResponse:
    name: str
    arguments: dict[str, Any]
    call_id: str = "call-1"


@dataclass(frozen=True)
class ModelFailure:
    error: Exception


class ScriptedModel:
    """Offline provider that returns a deterministic sequence and records requests."""

    context_window_tokens = 128_000
    max_output_tokens = 8_000

    def __init__(
        self,
        responses: Iterable[FinalResponse | ToolResponse | ModelFailure | LLMResponse],
        *,
        stream_chunks: Iterable[str] = (),
    ) -> None:
        self.responses = list(responses)
        self.stream_chunks = list(stream_chunks)
        self.calls: list[dict[str, Any]] = []
        self.streaming_calls: list[dict[str, Any]] = []

    def chat(self, **kwargs: Any) -> LLMResponse:
        self.calls.append(kwargs)
        return self._next()

    def stream_chat(self, *, on_text, **kwargs: Any) -> LLMResponse:
        self.streaming_calls.append(kwargs)
        response = self._next()
        chunks = self.stream_chunks or ([response.content] if response.content else [])
        for chunk in chunks:
            on_text(chunk)
        return response

    def _next(self) -> LLMResponse:
        if not self.responses:
            raise AssertionError("No scripted model response remains.")
        item = self.responses.pop(0)
        if isinstance(item, ModelFailure):
            raise item.error
        if isinstance(item, LLMResponse):
            return item
        if isinstance(item, FinalResponse):
            return LLMResponse(
                content=item.content,
                usage=item.usage,
                raw_message={"role": "assistant", "content": item.content},
            )
        call = ToolCall(id=item.call_id, name=item.name, arguments=item.arguments)
        return LLMResponse(
            content=None,
            tool_calls=[call],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": item.call_id,
                    "type": "function",
                    "function": {
                        "name": item.name,
                        "arguments": item.arguments,
                    },
                }],
            },
        )
