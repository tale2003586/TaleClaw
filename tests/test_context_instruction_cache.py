import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime.context import ContextBuilder, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.sessions.session import Session


class ContextInstructionCacheTests(unittest.TestCase):
    def test_instruction_file_uses_mtime_cache(self) -> None:
        ContextBuilder._instruction_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instruction = root / "AGENTS.md"
            instruction.write_text("cached project rules", encoding="utf-8")
            budgeter = ContextBudgeter.from_env()
            builder = ContextBuilder(
                budgeter=budgeter,
                prompt_assets_service=PromptAssetsService(
                    budgeter=budgeter,
                    instruction_root=root,
                ),
            )
            agent_spec = SimpleNamespace(instructions="base", tool_mode="coding")

            first = builder.build(session=Session(id="web:test"), agent_spec=agent_spec)
            self.assertIn("cached project rules", first.messages[0]["content"])

            with patch.object(Path, "read_text", side_effect=AssertionError("cache miss")):
                second = builder.build(session=Session(id="web:test"), agent_spec=agent_spec)

            self.assertIn("cached project rules", second.messages[0]["content"])

    def test_explicit_prefix_reuses_stable_instruction_block(self) -> None:
        ContextBuilder._instruction_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instruction = root / "AGENTS.md"
            instruction.write_text("stable prefix rules", encoding="utf-8")
            budgeter = ContextBudgeter.from_env()
            builder = ContextBuilder(
                budgeter=budgeter,
                prompt_assets_service=PromptAssetsService(
                    budgeter=budgeter,
                    instruction_root=root,
                ),
            )
            agent_spec = SimpleNamespace(instructions="base", tool_mode="coding")

            prefix = builder.build_prefix(agent_spec)
            self.assertFalse(prefix.cache_hit)

            instruction.write_text("changed after prefix build", encoding="utf-8")
            context = builder.build(
                session=Session(id="web:test"),
                agent_spec=agent_spec,
                prefix=prefix,
            )

            self.assertIn("stable prefix rules", context.messages[0]["content"])
            self.assertNotIn("changed after prefix build", context.messages[0]["content"])
            self.assertEqual(
                prefix.fingerprint,
                context.report.to_dict()["metadata"]["prefix_fingerprint"],
            )

    def test_prefix_cache_invalidates_when_instruction_file_changes(self) -> None:
        ContextBuilder._instruction_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            instruction = root / "AGENTS.md"
            instruction.write_text("first rules", encoding="utf-8")
            budgeter = ContextBudgeter.from_env()
            builder = ContextBuilder(
                budgeter=budgeter,
                prompt_assets_service=PromptAssetsService(
                    budgeter=budgeter,
                    instruction_root=root,
                ),
            )
            agent_spec = SimpleNamespace(instructions="base", tool_mode="coding")

            first = builder.build_prefix(agent_spec)
            second = builder.build_prefix(agent_spec)
            self.assertTrue(second.cache_hit)
            self.assertEqual(first.fingerprint, second.fingerprint)

            instruction.write_text("second rules with changed size", encoding="utf-8")
            third = builder.build_prefix(agent_spec)
            self.assertFalse(third.cache_hit)
            self.assertNotEqual(first.fingerprint, third.fingerprint)
            self.assertIn("second rules", third.system_prompt)


if __name__ == "__main__":
    unittest.main()
