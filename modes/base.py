from dataclasses import dataclass


@dataclass(frozen=True)
class ModeProfile:
    name: str
    system_prompt: str
    tool_mode: str
