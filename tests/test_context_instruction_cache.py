import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime.context import ContextBuilder
from sessions.session import Session


class ContextInstructionCacheTests(unittest.TestCase):
    def test_instruction_file_uses_mtime_cache(self) -> None:
        ContextBuilder._instruction_cache.clear()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".agent").mkdir()
            instruction = root / ".agent" / "assistant.md"
            instruction.write_text("cached assistant rules", encoding="utf-8")
            builder = ContextBuilder(instruction_root=root)
            profile = SimpleNamespace(system_prompt="base", tool_mode="bot")

            first = builder.build(session=Session(id="web:test"), profile=profile)
            self.assertIn("cached assistant rules", first.messages[0]["content"])

            with patch.object(Path, "read_text", side_effect=AssertionError("cache miss")):
                second = builder.build(session=Session(id="web:test"), profile=profile)

            self.assertIn("cached assistant rules", second.messages[0]["content"])


if __name__ == "__main__":
    unittest.main()
