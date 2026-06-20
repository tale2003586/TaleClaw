import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from plugins.base import Plugin
from plugins.plugin_manager import PluginManager
from tools.tool_registry import ToolRegistry


@dataclass(frozen=True)
class FrozenPlugin(Plugin):
    name: str = "frozen"
    seen_manager: object | None = None

    def setup(self, context):
        object.__setattr__(self, "context", context)
        object.__setattr__(self, "seen_manager", context.plugin_manager)


class PluginManagerTests(unittest.TestCase):
    def test_register_does_not_setattr_plugin_manager_on_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            plugin = FrozenPlugin()
            manager = PluginManager(
                [plugin],
                workspace=Path(tmp),
                tool_registry=ToolRegistry(),
            )

            self.assertIs(manager, plugin.seen_manager)
            self.assertFalse(hasattr(plugin, "_plugin_manager"))


if __name__ == "__main__":
    unittest.main()
