from __future__ import annotations

from runtime.trace.run_state import RunState

from .models import MinecraftTask, MinecraftTaskEvent


class TraceStoreMinecraftTraceSink:
    def __init__(self, trace_store, task: MinecraftTask) -> None:
        self.trace_store = trace_store
        self.run_state = RunState.create(
            run_id=task.trace_id or f"minecraft_{task.task_id}",
            session_id=task.session_id,
            mode="minecraft",
            execution_path="minecraft_worker",
            intent="minecraft_task",
            profile="minecraft",
            metadata={
                "trace_only": True,
                "minecraft_task_id": task.task_id,
                "bot_id": task.bot_id,
            },
        )
        self.trace_store.start_run(self.run_state)

    def emit(self, event: MinecraftTaskEvent) -> None:
        self.trace_store.append_event(
            self.run_state,
            event.event_type,
            {
                "minecraft_task_id": event.task_id,
                "minecraft_event_id": event.event_id,
                **event.payload,
            },
        )
