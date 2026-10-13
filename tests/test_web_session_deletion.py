import asyncio
import unittest
from http import HTTPStatus
from types import SimpleNamespace
from unittest.mock import patch

from runtime.sessions import SessionManager, SessionStore
from web.server import RequestHandler, _web_storage_id
from web.agent_service import AgentService, RunConflictError
from tests.postgres_utils import temporary_postgres_schema


class SessionDeletionStoreTests(unittest.TestCase):
    def test_store_delete_removes_session_and_messages(self) -> None:
        with temporary_postgres_schema("session_delete") as dsn:
            store = SessionStore(dsn)
            manager = SessionManager(dsn)
            session = manager.get_or_create("web:delete-me")
            session.add_message("user", "hello")
            manager.save(session)
            manager.close()

            isolated = SessionStore(dsn)
            self.assertTrue(isolated.delete_session("web:delete-me"))
            self.assertIsNone(isolated.load_session("web:delete-me"))
            self.assertFalse(isolated.delete_session("web:delete-me"))
            isolated.close()
            store.close()

    def test_manager_delete_evicts_loaded_session(self) -> None:
        with temporary_postgres_schema("manager_delete") as dsn:
            manager = SessionManager(dsn)
            session = manager.get_or_create("web:delete-me")
            session.add_message("user", "hello")
            manager.save(session)

            self.assertTrue(manager.delete("web:delete-me"))
            recreated = manager.get_or_create("web:delete-me")

            self.assertIsNot(session, recreated)
            self.assertEqual([], recreated.messages)
            manager.close()


class WebSessionDeletionTests(unittest.TestCase):
    def test_web_storage_id_accepts_plain_or_web_id(self) -> None:
        self.assertEqual("web:local:default", _web_storage_id("default"))
        self.assertEqual("web:local:default", _web_storage_id("web:local:default"))

    def test_web_storage_id_rejects_non_web_session(self) -> None:
        with self.assertRaisesRegex(ValueError, "Only Web sessions"):
            _web_storage_id("cli:default")

    def test_http_handler_deletes_web_session(self) -> None:
        calls = []

        class AgentService:
            def delete_session(self, session_id):
                calls.append(session_id)
                return True

        handler = object.__new__(RequestHandler)
        handler.agent_service = AgentService()
        handler._read_json_body = lambda: {"session_id": "default"}
        responses = []
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))

        with patch("web.server.read_sessions_page", return_value={"sessions": [{"id": "web:local:remaining"}], "has_more": False}):
            handler._handle_session_delete()

        self.assertEqual(["web:local:default"], calls)
        self.assertEqual(HTTPStatus.OK, responses[0][1])
        self.assertTrue(responses[0][0]["deleted"])
        self.assertEqual("default", responses[0][0]["session_id"])

    def test_http_handler_rejects_non_web_session(self) -> None:
        handler = object.__new__(RequestHandler)
        handler.agent_service = SimpleNamespace(delete_session=lambda session_id: True)
        handler._read_json_body = lambda: {"session_id": "task:private"}
        responses = []
        handler._send_json = lambda payload, status=HTTPStatus.OK: responses.append((payload, status))

        handler._handle_session_delete()

        self.assertEqual(HTTPStatus.BAD_REQUEST, responses[0][1])
        self.assertIn("Only Web sessions", responses[0][0]["error"])

    def test_http_methods_route_chat_and_session_delete_separately(self) -> None:
        handler = object.__new__(RequestHandler)
        handler.path = "/api/chat"
        handler._authorize = lambda: True
        routed = []
        handler._handle_chat = lambda: routed.append("chat")
        handler.do_POST()

        handler.path = "/api/session"
        handler._handle_session_delete = lambda: routed.append("delete")
        handler.do_DELETE()

        self.assertEqual(["chat", "delete"], routed)

    def test_inactive_agent_service_deletes_through_short_lived_store(self) -> None:
        import tempfile
        from pathlib import Path
        deleted = []
        with tempfile.TemporaryDirectory() as root:
            service = AgentService(root=Path(root), session_deleter=lambda value: deleted.append(value) or True)
            try:
                self.assertTrue(service.delete_session("web:local:default"))
                self.assertEqual(["web:local:default"], deleted)
            finally:
                service.stop()


# Actual same-session exclusion and cross-session process concurrency are
# exercised with spawned workers in test_background_agent_service.py. There is
# no retained async-runtime executor solely for private-method compatibility.


if __name__ == "__main__":
    unittest.main()
