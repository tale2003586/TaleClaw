import json
from types import SimpleNamespace

from applications.minecraft.models import MinecraftTask, ResourceGoal, TaskStatus
from plugins.minecraft import MinecraftPlugin
from tools.tool_registry import ToolRegistry


class Application:
    def __init__(self):
        self.tasks = {}

    def start_task(self, **kwargs):
        task = MinecraftTask(
            user_id=kwargs["user_id"],
            session_id=kwargs["session_id"],
            bot_id=kwargs["bot_id"],
            goal=kwargs["goal"],
            status=TaskStatus.OBSERVING,
        )
        self.tasks[task.task_id] = task
        return task

    def get_status(self, task_id, **_kwargs):
        return self.tasks[task_id]

    def cancel_task(self, task_id, **_kwargs):
        return task_id in self.tasks


def test_plugin_registers_only_four_external_tools_and_delegates():
    application = Application()
    plugin = MinecraftPlugin(application, bot_id="bot")
    registry = ToolRegistry()
    for registration in plugin.tools():
        registry.register(
            registration.schema,
            registration.handler,
            allowed_agents=registration.allowed_agents,
            session_scoped=registration.session_scoped,
        )
    session = SimpleNamespace(
        id="cli:test",
        metadata={"user_id": "user", "unlocked_tools": []},
    )
    assert registry.visible_names_for_turn(session, "minecraft") == {
        "minecraft_start_task",
        "minecraft_get_status",
        "minecraft_cancel_task",
        "minecraft_get_bot_status",
    }
    output = registry.execute(
        "minecraft_start_task",
        {"resource": "oak_log", "quantity": 4},
        session=session,
        mode="minecraft",
    )
    payload = json.loads(output)
    assert payload["goal"]["quantity"] == 4
    assert session.metadata["minecraft_task_id"] == payload["task_id"]
    assert registry.execution_error_for_turn(
        "minecraft_start_task",
        session=session,
        mode="bot",
    )
