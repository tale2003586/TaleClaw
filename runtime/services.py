from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeServices:
    """Shared TaleClaw Kernel services.

    AgentLoop is intentionally absent: it is an application execution strategy,
    not a Kernel service.
    """

    model_pool: Any
    model_task_runner: Any
    tool_registry: Any
    tool_executor: Any
    plugin_manager: Any
    memory_store: Any
    context_builder: Any
    session_manager: Any
    trace_store: Any
    cancellation_registry: Any
    message_bus: Any
