from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from tools.executor import ToolHook


@dataclass
class ToolRegistration:
    schema: dict
    handler: Callable[..., str]
    risk: str = "normal"
    enabled_modes: set[str] | None = None
    always_on: bool = False
    session_scoped: bool = False
    admin_only: bool = False
    source: str = "plugin"


@dataclass
class PluginContext:
    workspace: Path
    tool_registry: Any
    sessions: Any = None
    memory_store: Any = None
    plugin_manager: Any = None


@dataclass
class TurnContext:
    inbound: Any
    session: Any


@dataclass
class TurnResult:
    abort: bool = False
    reply: str = ""


@dataclass
class RunContext:
    run_state: Any
    session: Any
    run_dir: Path | None = None
    report: dict | None = None


@dataclass
class EvalContext:
    eval_dir: Path
    payload: dict


class Plugin:
    name: str = "plugin"

    def setup(self, context: PluginContext) -> None:
        self.context = context

    def tools(self) -> list[ToolRegistration]:
        return []

    def tool_hooks(self) -> list[ToolHook]:
        return []

    def before_turn(self, context: TurnContext) -> TurnResult | None:
        return None

    def after_turn(self, context: TurnContext, reply: str) -> None:
        return None

    def after_run(self, context: RunContext) -> None:
        return None

    def after_eval(self, context: EvalContext) -> None:
        return None
