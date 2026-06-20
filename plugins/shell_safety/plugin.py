from plugins.base import Plugin
from tools.hooks import ShellSafetyHook, ShellWorkspaceScopeHook


class ShellSafetyPlugin(Plugin):
    name = "shell_safety"

    def tool_hooks(self):
        return [ShellSafetyHook(), ShellWorkspaceScopeHook()]
