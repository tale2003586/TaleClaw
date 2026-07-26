"""Deterministic test doubles shared by the Phase 0 runtime baselines."""

from runtime.agent_spec import AgentSpec, ToolSet


def make_agent_spec(name: str, system_prompt: str, tool_mode: str):
    return AgentSpec(
        name=name,
        instructions=system_prompt,
        tool_set=ToolSet(mode=tool_mode),
    )
