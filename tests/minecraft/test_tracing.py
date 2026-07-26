from pathlib import Path

from applications.minecraft.models import MinecraftTask, MinecraftTaskEvent, ResourceGoal
from applications.minecraft.tracing import TraceStoreMinecraftTraceSink
from runtime.trace.trace_store import TraceStore


def test_minecraft_events_use_existing_trace_store(tmp_path: Path):
    store = TraceStore(tmp_path, index_enabled=False)
    task = MinecraftTask(
        task_id="task-1",
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )
    sink = TraceStoreMinecraftTraceSink(store, task)
    sink.emit(
        MinecraftTaskEvent(
            task_id=task.task_id,
            event_type="REASONING_GATE_DECISION",
            payload={"reason_code": "new_task", "plan_version": 0},
        )
    )
    trace = (store.run_dir(sink.run_state) / "trace.jsonl").read_text()
    assert "REASONING_GATE_DECISION" in trace
    assert "task-1" in trace
