from types import SimpleNamespace

from applications.minecraft.models import (
    MinecraftTask,
    MinecraftTaskEvent,
    ResourceGoal,
    TaskStatus,
)
from applications.minecraft.progress import MinecraftProgressPublisher
from applications.minecraft.session_adapter import MinecraftSessionAdapter


class Sessions:
    def __init__(self):
        self.session = SimpleNamespace(id="s", metadata={})

    def get_or_create(self, _session_id):
        return self.session

    def save(self, _session):
        return None


def test_session_only_keeps_task_link_and_progress_is_throttled():
    sessions = Sessions()
    adapter = MinecraftSessionAdapter(sessions)
    adapter.bind_task("s", "task-1")
    assert sessions.session.metadata == {"minecraft_task_id": "task-1"}
    published = []
    publisher = MinecraftProgressPublisher(published.append, every=2)
    task = MinecraftTask(
        task_id="task-1",
        user_id="u",
        session_id="s",
        bot_id="b",
        goal=ResourceGoal(resource="oak_log", quantity=4),
    )
    publisher.publish(task, MinecraftTaskEvent(task_id=task.task_id, event_type="one"))
    assert published == []
    publisher.publish(task, MinecraftTaskEvent(task_id=task.task_id, event_type="two"))
    assert published[-1]["task_id"] == "task-1"
