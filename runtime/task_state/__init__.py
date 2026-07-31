"""Shared runtime task state for chat, hybrid, and coding agents."""

from .models import (
    TASK_STATE_METADATA_KEY,
    TASK_STATE_SCHEMA_VERSION,
    TaskAction,
    TaskBlocker,
    TaskProgressItem,
    TaskStateCore,
    TaskStatus,
)
from .patch import (
    TaskStateCorePatch,
    TaskStateValidationError,
    apply_task_state_core_patch,
    validate_task_state_core_patch,
)
from .rendering import render_task_state_core_message
from .service import (
    ensure_task_state_core,
    load_task_state_core,
    save_task_state_core,
)

__all__ = (
    "TASK_STATE_METADATA_KEY",
    "TASK_STATE_SCHEMA_VERSION",
    "TaskAction",
    "TaskBlocker",
    "TaskProgressItem",
    "TaskStateCore",
    "TaskStateCorePatch",
    "TaskStateValidationError",
    "TaskStatus",
    "apply_task_state_core_patch",
    "ensure_task_state_core",
    "load_task_state_core",
    "render_task_state_core_message",
    "save_task_state_core",
    "validate_task_state_core_patch",
)
