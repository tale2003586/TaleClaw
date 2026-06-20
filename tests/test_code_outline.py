import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sessions.session import Session
from tools import handlers
from tools.tool_registry import build_lead_tool_registry


class CodeOutlineToolTests(unittest.TestCase):
    def test_code_outline_extracts_python_symbols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "app.py").write_text(
                "\n".join([
                    "class Service:",
                    "    def run(self):",
                    "        pass",
                    "",
                    "async def main():",
                    "    pass",
                ]),
                encoding="utf-8",
            )
            session = Session(id="task:outline-python", metadata={"workspace_root": str(workspace)})

            payload = json.loads(handlers.run_code_outline("app.py", _session=session))

            symbols = {(item["name"], item["kind"], item["line"]) for item in payload["symbols"]}
            self.assertIn(("Service", "class", 1), symbols)
            self.assertIn(("run", "method", 2), symbols)
            self.assertIn(("main", "function", 5), symbols)
            self.assertEqual(6, payload["total_lines"])

    def test_code_outline_extracts_typescript_symbols_and_uses_cache(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "main.tsx").write_text(
                "\n".join([
                    "export class Dashboard {",
                    "  render() {",
                    "    return null",
                    "  }",
                    "}",
                    "export function boot() { return null }",
                    "const App = () => null",
                ]),
                encoding="utf-8",
            )
            session = Session(id="task:outline-ts", metadata={"workspace_root": str(workspace)})

            payload = json.loads(handlers.run_code_outline("main.tsx", _session=session))
            cached = handlers.run_code_outline("main.tsx", _session=session)

            symbols = {(item["name"], item["kind"]) for item in payload["symbols"]}
            self.assertIn(("Dashboard", "class"), symbols)
            self.assertIn(("render", "method"), symbols)
            self.assertIn(("boot", "function"), symbols)
            self.assertIn(("App", "function"), symbols)
            self.assertIn("[tool-cache] already read", cached)

    def test_code_outline_paginates_large_symbol_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            (workspace / "many.py").write_text(
                "\n".join(f"def func_{index}(): pass" for index in range(60)),
                encoding="utf-8",
            )
            session = Session(id="task:outline-page", metadata={"workspace_root": str(workspace)})

            with patch.object(handlers, "CODE_OUTLINE_MAX_CHARS", 700):
                output = handlers.run_code_outline("many.py", _session=session)

            self.assertIn("To continue: code_outline(", output)
            self.assertIn("offset=", output)

    def test_code_outline_is_visible_for_coding_lead(self) -> None:
        registry = build_lead_tool_registry()
        session = Session(id="task:outline-visible", current_mode="coding")

        visible = registry.visible_names_for_turn(session, "coding")

        self.assertIn("code_outline", visible)


if __name__ == "__main__":
    unittest.main()
