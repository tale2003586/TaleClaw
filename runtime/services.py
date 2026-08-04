from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RuntimeServices:
    """Shared TaleClaw Kernel services.

    TurnCoordinator is intentionally absent: it is an application service,
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
    semantic_memory_repository: Any = None
    semantic_memory_command_service: Any = None
    semantic_memory_retrieval_service: Any = None
    semantic_memory_index_synchronizer: Any = None
