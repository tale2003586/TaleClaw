from agents.minecraft import MINECRAFT_AGENT_SPEC
from tools.policy import PRELOADED_TOOLS_BY_MODE


def test_minecraft_agent_is_tool_isolated_and_cannot_spawn():
    assert MINECRAFT_AGENT_SPEC.tool_mode == "minecraft"
    assert not MINECRAFT_AGENT_SPEC.spawn_policy.enabled
    assert PRELOADED_TOOLS_BY_MODE["minecraft"] == {
        "minecraft_start_task",
        "minecraft_get_status",
        "minecraft_cancel_task",
        "minecraft_get_bot_status",
    }
    assert "bash" not in PRELOADED_TOOLS_BY_MODE["minecraft"]
    assert "task" not in PRELOADED_TOOLS_BY_MODE["minecraft"]
