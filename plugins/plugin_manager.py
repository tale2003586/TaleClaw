from pathlib import Path
from typing import Iterable
from weakref import WeakKeyDictionary

from .base import EvalContext, Plugin, PluginContext, RunContext, TurnContext, TurnResult


class PluginManager:
    def __init__(
        self,
        plugins: Iterable[Plugin] | None = None,
        *,
        workspace: Path,
        tool_registry,
        sessions=None,
    ) -> None:
        self.workspace = workspace
        self.tool_registry = tool_registry
        self.sessions = sessions
        self.plugins: list[Plugin] = []
        self.loaded_names: list[str] = []
        self._tool_names: list[str] = []
        self._tool_hooks = []
        self._plugin_refs = WeakKeyDictionary()

        for plugin in plugins or []:
            self.register(plugin)

    @property
    def tool_hooks(self) -> list:
        return list(self._tool_hooks)

    def register(self, plugin: Plugin) -> None:
        context = PluginContext(
            workspace=self.workspace,
            tool_registry=self.tool_registry,
            sessions=self.sessions,
            plugin_manager=self,
        )
        plugin.setup(context)
        try:
            self._plugin_refs[plugin] = self
        except TypeError:
            pass
        self.plugins.append(plugin)
        self.loaded_names.append(plugin.name)

        for tool in plugin.tools():
            self.tool_registry.register(tool)
            self._tool_names.append(tool.schema["function"]["name"])

        self._tool_hooks.extend(plugin.tool_hooks())

    def before_turn(self, inbound, session) -> TurnResult:
        context = TurnContext(inbound=inbound, session=session)
        for plugin in self.plugins:
            result = plugin.before_turn(context)
            if result and result.abort:
                return result
        return TurnResult()

    def after_turn(self, inbound, session, reply: str) -> None:
        context = TurnContext(inbound=inbound, session=session)
        for plugin in self.plugins:
            plugin.after_turn(context, reply)

    def after_run(
        self,
        *,
        run_state,
        session,
        run_dir: Path | None = None,
        report: dict | None = None,
    ) -> None:
        context = RunContext(
            run_state=run_state,
            session=session,
            run_dir=run_dir,
            report=report,
        )
        for plugin in self.plugins:
            plugin.after_run(context)

    def after_eval(self, *, eval_dir: Path, payload: dict) -> None:
        context = EvalContext(eval_dir=eval_dir, payload=payload)
        for plugin in self.plugins:
            plugin.after_eval(context)

    def status_text(self) -> str:
        if not self.loaded_names:
            return "Plugins: none"
        lines = ["Plugins:"]
        for name in self.loaded_names:
            lines.append(f"- {name}")
        if self._tool_names:
            lines.append("")
            lines.append("Plugin tools:")
            for name in self._tool_names:
                lines.append(f"- {name}")
        if self._tool_hooks:
            lines.append("")
            lines.append("Plugin hooks:")
            for hook in self._tool_hooks:
                lines.append(f"- {hook.name}")
        return "\n".join(lines)
