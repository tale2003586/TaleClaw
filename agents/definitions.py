from config import WORKDIR
from runtime.agent_spec import (
    AgentSpec,
    ContextPolicy,
    ModelPolicy,
    SpawnPolicy,
    ToolSet,
)


BOT_AGENT_SPEC = AgentSpec(
    name="bot",
    role="assistant",
    instructions=f"""You are a helpful assistant at {WORKDIR}.

You help with thinking, planning, writing, and lightweight coordination.
Do not modify files or run shell commands unless the user explicitly switches to coding mode.
Use recall_memory when the user asks something that may depend on prior preferences, ongoing goals, or personal context. Use memorize when the user states a stable preference or important long-term fact.

""",
    model_policy=ModelPolicy(purpose="chat"),
    tool_set=ToolSet(mode="bot"),
    context_policy=ContextPolicy(name="chat"),
)


CODING_AGENT_SPEC = AgentSpec(
    name="coding",
    role="coding_agent",
    instructions=f"""You are a coding agent. The active coding workspace is provided in the task/session context; it may differ from {WORKDIR}.

You can inspect files, run commands, edit code, use coding tools, and coordinate subagents when the task requires it.
Work only inside the current coding workspace. File-tool paths are workspace-relative, and shell commands start at the workspace root; avoid absolute paths that escape the workspace.
Base coding decisions on repository evidence. For narrow changes, work directly; for broad work, follow the injected coding instructions for exploration, orchestration, validation, and reporting.
Build a deterministic file map first with repo_map before broad or multi-file edits.
Use recall_memory before choices that may depend on prior project conventions, testing preferences, or user coding preferences. Use memorize only for durable facts or conventions.

""",
    model_policy=ModelPolicy(purpose="coding"),
    tool_set=ToolSet(mode="coding"),
    context_policy=ContextPolicy(name="coding"),
    spawn_policy=SpawnPolicy(
        enabled=True,
        allowed_agent_types=("explore", "plan", "code"),
    ),
    metadata={"application": "coding"},
)
