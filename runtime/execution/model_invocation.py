"""Stateless model invocation selected by the reasoning state machine."""

from __future__ import annotations

from typing import Any, Callable

from runtime.ports import ModelPort


def supports_streaming(
    provider: ModelPort,
    on_text: Callable[[str], None] | None,
) -> bool:
    return on_text is not None and hasattr(provider, "stream_chat")


def invoke_model(
    provider: ModelPort,
    *,
    model: str,
    messages: list[dict],
    tools: list[dict],
    max_tokens: int,
    on_text: Callable[[str], None] | None = None,
    thinking_enabled: bool = False,
) -> Any:
    if supports_streaming(provider, on_text):
        return provider.stream_chat(
            model=model,
            messages=messages,
            tools=tools,
            tool_choice="auto",
            max_tokens=max_tokens,
            on_text=on_text,
            thinking_enabled=thinking_enabled,
        )
    return provider.chat(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
        max_tokens=max_tokens,
        thinking_enabled=thinking_enabled,
    )


__all__ = ("invoke_model", "supports_streaming")
