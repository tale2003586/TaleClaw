"""Structural ports at the Agent Runtime kernel boundary.

The protocols deliberately contain only operations consumed by the core path.
Concrete applications and optional services may implement them without being
imported by ``runtime.execution``.
"""

from __future__ import annotations

from typing import Any, Callable, Protocol, runtime_checkable


@runtime_checkable
class ContextPort(Protocol):
    def build_prefix(
        self,
        profile: Any,
        *,
        session: Any,
        active_turn_start_index: int | None,
    ) -> Any: ...

    def build(self, *, session: Any, profile: Any, **kwargs: Any) -> Any: ...


@runtime_checkable
class ModelPort(Protocol):
    def chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str,
        max_tokens: int,
        thinking_enabled: bool = False,
    ) -> Any: ...

    def stream_chat(
        self,
        *,
        model: str,
        messages: list[dict],
        tools: list[dict],
        tool_choice: str,
        max_tokens: int,
        on_text: Callable[[str], None],
        thinking_enabled: bool = False,
    ) -> Any: ...


@runtime_checkable
class ToolPort(Protocol):
    def spec_for(self, name: str) -> Any: ...

    def schemas_for_turn(self, session: Any, mode: str) -> list[dict]: ...

    def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        session: Any,
        **kwargs: Any,
    ) -> str: ...


@runtime_checkable
class ToolExecutorPort(Protocol):
    def execute(self, request: Any, invoker: Callable[[str, dict], str]) -> Any: ...


@runtime_checkable
class ObservabilityPort(Protocol):
    def append_event(
        self,
        run_state: Any,
        event: str,
        payload: dict[str, Any],
        **kwargs: Any,
    ) -> Any: ...

    def write_run_state(self, run_state: Any) -> Any: ...


__all__ = (
    "ContextPort",
    "ModelPort",
    "ObservabilityPort",
    "ToolExecutorPort",
    "ToolPort",
)
