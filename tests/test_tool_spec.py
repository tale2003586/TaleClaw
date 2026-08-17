from pathlib import Path
import tempfile
import unittest

from plugins.base import Plugin
from plugins.plugin_manager import PluginManager
from runtime.execution.recovery import RecoveryController
from runtime.execution.state import RunExecutionState
from models.provider import ToolCall
from tools.schema import LEAD_TOOLS, SEARCH_TOOLS, TEAMMATE_TOOLS, function_tool
from tools.spec import ToolExposure, ToolRisk, ToolSpec, ToolStateEffect
from tools.tool_registry import (
    BUILTIN_TOOL_DECLARATIONS,
    BuiltinToolDeclaration,
    ToolRegistry,
    build_builtin_registry,
    build_lead_tool_registry,
    build_teammate_tool_registry,
    index_builtin_declarations,
)
from runtime.sessions import Session


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
            exposure=ToolExposure.DEFERRED,
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
        self.assertEqual(tool_spec.exposure.value, catalog["exposure"])
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

    def test_mode_specific_schema_is_owned_by_the_tool_spec(self):
        base = function_tool("state", "coding state", {"coding": {"type": "string"}}, [])
        bot = function_tool("state", "bot state", {"bot": {"type": "string"}}, [])
        tool_spec = ToolSpec(
            schema=base,
            handler=lambda **_: "ok",
            schemas_by_mode={"bot": bot},
        )
        registry = ToolRegistry()
        registry.register(tool_spec)

        self.assertIs(bot, tool_spec.schema_for("bot"))
        self.assertIs(base, tool_spec.schema_for("coding"))
        self.assertEqual(bot, registry._schema_for_mode(tool_spec, "bot"))

    def test_default_chat_surface_only_exposes_capability_search(self):
        registry = build_lead_tool_registry()
        session = Session(id="chat")

        visible = registry.visible_names_for_turn(session, "bot")
        self.assertEqual({"tool_search"}, visible)
        search = registry.execute(
            "tool_search",
            {"query": "long-term semantic memory"},
            session=session,
            mode="bot",
        )
        self.assertIn("recall_memory", search)
        self.assertNotIn("memorize", search)

    def test_default_coding_surface_is_limited_to_primitives(self):
        registry = build_lead_tool_registry()
        session = Session(id="coding")

        self.assertEqual(
            {
                "bash", "list_files", "rg", "read_file", "write_file",
                "read_files", "code_outline", "edit_file", "tool_search",
            },
            registry.visible_names_for_turn(session, "coding"),
        )

    def test_specialized_tool_can_be_unlocked_for_one_turn(self):
        registry = build_lead_tool_registry()
        session = Session(id="chat")

        result = registry.execute(
            "tool_search",
            {"query": "What preferences did I mention before?"},
            session=session,
            mode="bot",
        )

        self.assertIn("unlocked for this turn", result)
        visible = registry.visible_names_for_turn(session, "bot")
        self.assertIn("tool_search", visible)
        self.assertIn("recall_memory", visible)
        self.assertNotIn("memorize", visible)

    def test_builtin_registry_registers_every_schema_exactly_once(self):
        lead = build_lead_tool_registry()
        teammate = build_teammate_tool_registry("spec-test")

        self.assertEqual(52, len(lead._tools))
        self.assertEqual(34, len(teammate._tools))
        self.assertEqual(
            {schema["function"]["name"] for schema in LEAD_TOOLS},
            set(lead._tools),
        )
        self.assertEqual(
            {schema["function"]["name"] for schema in LEAD_TOOLS},
            {declaration.name for declaration in BUILTIN_TOOL_DECLARATIONS},
        )
        self.assertEqual(
            {schema["function"]["name"] for schema in TEAMMATE_TOOLS + SEARCH_TOOLS},
            set(teammate._tools),
        )
        self.assertEqual("tool_search", lead.spec_for("tool_search").name)
        self.assertEqual("tool_search", teammate.spec_for("tool_search").name)

    def test_builtin_registry_fails_fast_for_missing_handler(self):
        schema = function_tool("missing_handler", "missing", {}, [])
        declarations = {
            "missing_handler": BuiltinToolDeclaration("missing_handler"),
        }

        with self.assertRaisesRegex(ValueError, "no handler: missing_handler"):
            build_builtin_registry(
                schemas=(schema,),
                handlers={},
                source="test",
                declarations=declarations,
            )

    def test_builtin_declaration_duplicate_and_schema_mismatch_fail_fast(self):
        duplicate = BuiltinToolDeclaration("duplicate")
        with self.assertRaisesRegex(ValueError, "Duplicate builtin tool declaration"):
            index_builtin_declarations((duplicate, duplicate))

        with self.assertRaisesRegex(ValueError, "does not match schema"):
            BuiltinToolDeclaration("declared").bind(
                function_tool("actual", "actual", {}, []),
                lambda **_: "ok",
                source="test",
            )

    def test_builtin_registry_rejects_duplicate_schemas_and_orphaned_handlers(self):
        schema = function_tool("declared", "declared", {}, [])
        declarations = {"declared": BuiltinToolDeclaration("declared")}
        handler = lambda **_: "ok"

        with self.assertRaisesRegex(ValueError, "Duplicate builtin schema declaration"):
            build_builtin_registry(
                schemas=(schema, schema),
                handlers={"declared": handler},
                source="test",
                declarations=declarations,
            )
        with self.assertRaisesRegex(ValueError, "handlers have no registered schema"):
            build_builtin_registry(
                schemas=(schema,),
                handlers={"declared": handler, "orphan": handler},
                source="test",
                declarations=declarations,
            )


if __name__ == "__main__":
    unittest.main()
