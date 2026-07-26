from runtime.agent_spec import (
    AgentSpec,
    ContextPolicy,
    ModelPolicy,
    RunLimits,
    SpawnPolicy,
    ToolSet,
)


MINECRAFT_AGENT_SPEC = AgentSpec(
    name="minecraft",
    role="minecraft_task_coordinator",
    instructions="""You coordinate Minecraft resource tasks.

Use only minecraft_start_task, minecraft_get_status, minecraft_cancel_task, and
minecraft_get_bot_status.  The MinecraftWorker owns all long-running game
actions and cognition after task creation.  Never use shell, file tools,
subagents, raw packets, arbitrary network calls, commands, attacks, containers,
explosives, or lava actions.  Reject non-resource requests.
""",
    model_policy=ModelPolicy(purpose="chat"),
    tool_set=ToolSet(mode="minecraft"),
    context_policy=ContextPolicy(
        name="minecraft_entry",
        include_memory=False,
        include_history=True,
        include_skills=False,
    ),
    limits=RunLimits(max_tokens=800, max_reasoning_steps=3, max_tool_calls=2),
    spawn_policy=SpawnPolicy(enabled=False),
    metadata={"application": "minecraft"},
)
