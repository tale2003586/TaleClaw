from pathlib import Path
import tempfile
import unittest

from plugins.base import Plugin
from plugins.plugin_manager import PluginManager
from runtime.execution.recovery import RecoveryController
from runtime.execution.state import RunExecutionState
from models.provider import ToolCall
from tools.schema import function_tool
from tools.spec import ToolInjection, ToolRisk, ToolSpec, ToolStateEffect
from tools.tool_registry import ToolRegistry


class SpecPlugin(Plugin):
    name = "spec-plugin"

    def __init__(self, tool_spec):
        self.tool_spec = tool_spec

    def tools(self):
        return [self.tool_spec]


class ToolSpecTests(unittest.TestCase):
    def make_spec(self):
        return ToolSpec(
            schema=function_tool("publish", "publish result", {}, []),
            handler=lambda **_: "published",
            allowed_modes=frozenset({"coding"}),
            risk=ToolRisk.HIGH,
            idempotent=False,
            side_effect=True,
            state_effect=ToolStateEffect.EXTERNAL,
            injection=ToolInjection.DEFERRED,
            session_scoped=True,
            admin_only=True,
            policy_tag="artifact.publish",
            runtime_parameters=frozenset({"_trace_store"}),
        )

    def test_registry_catalog_and_policy_read_one_spec_instance(self):
        registry = ToolRegistry()
        tool_spec = self.make_spec()
        registry.register(tool_spec)

        self.assertIs(tool_spec, registry.spec_for("publish"))
        catalog = registry.catalog()[0]
        self.assertEqual(tool_spec.schema["function"]["description"], catalog["description"])
        self.assertEqual(tool_spec.risk.value, catalog["risk"])
        self.assertEqual(tool_spec.injection.value, catalog["injection"])
        self.assertEqual(tool_spec.idempotent, catalog["idempotent"])
        self.assertEqual(tool_spec.side_effect, catalog["side_effect"])
        self.assertNotIn("publish", registry.policy.visible_tools(None, "coding"))
        self.assertFalse(tool_spec.enabled_for("bot"))

    def test_permission_injection_and_runtime_parameters_come_from_spec(self):
        tool_spec = self.make_spec()
        guest = type("Session", (), {"metadata": {"user_role": "user"}})()
        admin = type("Session", (), {"metadata": {"user_role": "admin"}})()
        self.assertFalse(tool_spec.enabled_for("coding", guest))
        self.assertTrue(tool_spec.enabled_for("coding", admin))
        self.assertTrue(tool_spec.session_scoped)
        self.assertEqual(frozenset({"_trace_store"}), tool_spec.runtime_parameters)
        self.assertTrue(tool_spec.requires_audit)

    def test_recovery_reads_the_registry_spec_without_parallel_metadata(self):
        registry = ToolRegistry()
        tool_spec = self.make_spec()
        registry.register(tool_spec)
        provider = type("Provider", (), {"calls": []})()
        decision = RecoveryController().duplicate_tool_call(
            calls=[ToolCall(id="1", name="publish", arguments={})],
            specs=[registry.spec_for("publish")],
            state=RunExecutionState(),
            provider=provider,
            model="judge",
        )
        self.assertEqual("repeated_side_effect_risk", decision.reason.value)
        self.assertEqual([], provider.calls)

    def test_plugin_registration_uses_the_same_tool_spec(self):
        registry = ToolRegistry()
        tool_spec = self.make_spec()
        with tempfile.TemporaryDirectory() as tmp:
            PluginManager(
                [SpecPlugin(tool_spec)],
                workspace=Path(tmp),
                tool_registry=registry,
            )
        self.assertIs(tool_spec, registry.spec_for("publish"))


if __name__ == "__main__":
    unittest.main()
