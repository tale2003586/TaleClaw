import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from runtime.routing.router import ModeRouter
from runtime.context import ContextBuilder
from memory.lifecycle import MemoryLifecycle
from memory.scoped_store import ScopedMemoryStore
from plugins.markdown_pdf.plugin import MarkdownPdfPlugin
from sessions import Session, SessionManager
from postgres_utils import temporary_postgres_schema
from tools import handlers
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry, build_lead_tool_registry
from web import server


def _session(user_id: str, *, role: str = "user") -> Session:
    return Session(
        id=f"web:{user_id}:default",
        current_mode="bot",
        metadata={"user_id": user_id, "user_role": role},
    )


class MultiUserIsolationTests(unittest.TestCase):
    def test_web_users_json_supports_admin_and_regular_users(self) -> None:
        payload = json.dumps({
            "admin": {"password": "admin-secret", "role": "admin"},
            "guest": {"password": "guest-secret", "role": "user"},
        })
        with patch.dict("os.environ", {"WEB_USERS_JSON": payload}):
            users = server.auth_users()

        self.assertEqual("admin", users["admin"].role)
        self.assertEqual("user", users["guest"].role)

    def test_web_storage_and_analysis_records_are_separate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with patch.object(server, "ROOT", root):
                alice_upload = server._safe_storage_path("uploads/note.txt", user_id="alice")
                alice_upload.parent.mkdir(parents=True)
                alice_upload.write_text("alice-only", encoding="utf-8")
                server.append_analysis_record(
                    user_text="alice input",
                    assistant_text="alice reply",
                    user_id="alice",
                )
                server.append_analysis_record(
                    user_text="bob input",
                    assistant_text="bob reply",
                    user_id="bob",
                )

                alice_files = server.list_storage("uploads", user_id="alice")
                bob_files = server.list_storage("", user_id="bob")
                alice_record = server._analysis_record_path("alice").read_text()
                bob_record = server._analysis_record_path("bob").read_text()

            self.assertEqual("uploads/note.txt", alice_files["entries"][0]["path"])
            self.assertNotIn("uploads", [item["name"] for item in bob_files["entries"]])
            self.assertIn("alice input", alice_record)
            self.assertNotIn("bob input", alice_record)
            self.assertIn("bob input", bob_record)

    def test_web_session_queries_only_return_current_users_rows(self) -> None:
        with temporary_postgres_schema("multi_user_sessions") as dsn:
            manager = SessionManager(dsn)
            for user_id in ("alice", "bob"):
                session = manager.get_or_create(f"web:{user_id}:default")
                session.metadata["user_id"] = user_id
                session.add_message("user", f"{user_id}-only")
                manager.save(session)
            manager.close()

            with patch.dict("os.environ", {"SESSION_DATABASE_URL": dsn}):
                alice_sessions = server.read_sessions("alice")
                alice_chat = server.read_session("default", user_id="alice")
                with self.assertRaisesRegex(ValueError, "current user"):
                    server.read_session("web:bob:default", user_id="alice")

            self.assertEqual(["default"], [item["chat_id"] for item in alice_sessions])
            self.assertEqual("alice-only", alice_chat["messages"][0]["content"])

    def test_bot_storage_and_memory_tools_follow_session_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            registry = build_lead_tool_registry()
            alice = _session("alice")
            bob = _session("bob")

            with patch.object(handlers, "WORKDIR", workspace):
                registry.execute(
                    "storage_write_file",
                    {"path": "reports/daily.md", "content": "alice-only"},
                    session=alice,
                    mode="bot",
                )
                registry.execute(
                    "memorize",
                    {"content": "alice preference"},
                    session=alice,
                    mode="bot",
                )
                bob_listing = json.loads(handlers.run_storage_list(_session=bob))
                bob_memory = handlers.run_recall_memory(_session=bob)

            alice_root = workspace / ".users" / "alice"
            self.assertTrue((alice_root / "storage" / "generated" / "reports" / "daily.md").is_file())
            self.assertEqual([], bob_listing["entries"])
            self.assertNotIn("alice preference", bob_memory)

    def test_context_and_lifecycle_use_current_users_memory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            stores = ScopedMemoryStore(workspace)
            alice = _session("alice")
            bob = _session("bob")
            stores.for_session(alice).append("memory", "alice-only memory")
            stores.for_session(bob).append("memory", "bob-only memory")

            profile = SimpleNamespace(system_prompt="system")
            context = ContextBuilder(memory_store=stores).build(
                session=alice,
                profile=profile,
            )
            alice.add_message("user", "remember this turn")
            alice.add_message("assistant", "noted")
            MemoryLifecycle(stores).after_turn(alice)

            rendered = json.dumps(context.messages, ensure_ascii=False)
            alice_history = stores.for_session(alice).read_history()
            bob_history = stores.for_session(bob).read_history()

            self.assertIn("alice-only memory", rendered)
            self.assertNotIn("bob-only memory", rendered)
            self.assertIn("remember this turn", alice_history)
            self.assertNotIn("remember this turn", bob_history)

    def test_regular_user_cannot_enter_coding_or_see_admin_only_tool(self) -> None:
        user_session = _session("guest")
        route = ModeRouter().route(user_session, "/coding")

        registry = ToolRegistry()
        registry.register(
            function_tool("server_admin", "admin operation", {}),
            lambda **kwargs: "ok",
            enabled_modes={"bot"},
            always_on=True,
            admin_only=True,
        )

        self.assertEqual("bot", user_session.current_mode)
        self.assertTrue(route.switched)
        self.assertNotIn("server_admin", registry.visible_names_for_turn(user_session, "bot"))

    def test_markdown_pdf_stays_inside_users_private_storage(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            alice = _session("alice")
            source = workspace / ".users" / "alice" / "storage" / "note.md"
            source.parent.mkdir(parents=True)
            source.write_text("# Alice", encoding="utf-8")
            plugin = MarkdownPdfPlugin()
            plugin.setup(SimpleNamespace(workspace=workspace))

            def render(**kwargs):
                kwargs["output_path"].write_bytes(b"%PDF-private")
                return SimpleNamespace(local_images=0, skipped_images=0)

            with patch("plugins.markdown_pdf.plugin.render_markdown_pdf", render):
                result = json.loads(plugin.markdown_to_pdf("note.md", _session=alice))

            self.assertEqual("storage/generated/note.pdf", result["output_path"])
            self.assertTrue(
                (workspace / ".users" / "alice" / "storage" / "generated" / "note.pdf").is_file()
            )
            self.assertFalse((workspace / "storage" / "generated" / "note.pdf").exists())


if __name__ == "__main__":
    unittest.main()
