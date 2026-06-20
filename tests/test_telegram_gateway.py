import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from bus.events import InboundMessage, OutboundMessage
from runtime.agent_loop import AgentLoop
from gateway.telegram.adapter import TelegramGateway
from gateway.telegram.client import TelegramBotApiClient, split_telegram_text
from gateway.telegram.identity import TelegramIdentityResolver
from gateway.telegram.store import (
    TelegramGatewayStore,
    build_runtime_chat_id,
    external_chat_id_from_runtime,
)
from sessions import Session
from postgres_utils import temporary_postgres_schema


class TelegramIdentityResolverTests(unittest.TestCase):
    def test_unconfigured_resolver_rejects_user(self):
        self.assertIsNone(TelegramIdentityResolver().resolve(123))

    def test_allowed_user_gets_isolated_regular_account(self):
        identity = TelegramIdentityResolver(allowed_user_ids={123}).resolve(123)

        self.assertIsNotNone(identity)
        self.assertEqual(identity.user_id, "telegram_123")
        self.assertEqual(identity.role, "user")

    def test_explicit_environment_map_can_bind_admin(self):
        with patch.dict(
            "os.environ",
            {
                "TELEGRAM_ALLOWED_USER_IDS": "",
                "TELEGRAM_USER_MAP": json.dumps({
                    "123": {"user_id": "admin", "role": "admin"},
                }),
            },
            clear=True,
        ):
            identity = TelegramIdentityResolver.from_env().resolve(123)

        self.assertIsNotNone(identity)
        self.assertEqual(identity.user_id, "admin")
        self.assertEqual(identity.role, "admin")

    def test_wildcard_only_grants_regular_role(self):
        with patch.dict(
            "os.environ",
            {"TELEGRAM_ALLOWED_USER_IDS": "*", "TELEGRAM_USER_MAP": ""},
            clear=True,
        ):
            identity = TelegramIdentityResolver.from_env().resolve(456)

        self.assertEqual(identity.user_id, "telegram_456")
        self.assertEqual(identity.role, "user")


class TelegramGatewayStoreTests(unittest.TestCase):
    def test_offset_and_active_conversation_are_persistent(self):
        with temporary_postgres_schema("telegram_store") as dsn:
            store = TelegramGatewayStore(dsn)
            self.assertIsNone(store.get_offset())
            self.assertEqual(store.get_conversation_id(123), "default")
            first_runtime_id = store.runtime_chat_id(123)
            next_conversation = store.start_conversation(123)
            store.set_offset(42)
            store.close()

            reopened = TelegramGatewayStore(dsn)
            try:
                self.assertEqual(reopened.get_offset(), 42)
                self.assertEqual(reopened.get_conversation_id(123), next_conversation)
                self.assertNotEqual(reopened.runtime_chat_id(123), first_runtime_id)
            finally:
                reopened.close()

    def test_runtime_chat_id_round_trip(self):
        runtime_chat_id = build_runtime_chat_id(123, "default")

        self.assertEqual(runtime_chat_id, "tg_123_default")
        self.assertEqual(external_chat_id_from_runtime(runtime_chat_id), 123)

    def test_existing_outbox_schema_is_migrated_for_documents(self):
        with temporary_postgres_schema("telegram_schema") as dsn:
            import psycopg

            with psycopg.connect(dsn) as conn:
                conn.execute(
                    """
                    CREATE TABLE telegram_outbox (
                        id         BIGSERIAL PRIMARY KEY,
                        chat_id    TEXT NOT NULL,
                        text       TEXT NOT NULL,
                        status     TEXT NOT NULL DEFAULT 'pending',
                        attempts   INTEGER NOT NULL DEFAULT 0,
                        source     TEXT,
                        metadata   TEXT NOT NULL DEFAULT '{}',
                        error      TEXT,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        sent_at    TEXT
                    )
                    """
                )

            store = TelegramGatewayStore(dsn)
            try:
                store.enqueue_document(
                    chat_id=123,
                    document_path="storage/reports/daily.md",
                    caption="daily",
                )
                pending = store.list_pending_messages()
            finally:
                store.close()

        self.assertEqual("document", pending[0]["message_type"])
        self.assertEqual("storage/reports/daily.md", pending[0]["document_path"])
        self.assertEqual("daily", pending[0]["caption"])


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_updates_sends_offset_timeout_and_allowed_updates(self):
        captured = {}

        async def handler(request):
            captured["path"] = request.url.path
            captured.update(json.loads(request.content))
            return httpx.Response(200, json={"ok": True, "result": []})

        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = TelegramBotApiClient("test-token", http_client=http_client)
            updates = await client.get_updates(offset=8, timeout=12)

        self.assertEqual(updates, [])
        self.assertEqual(captured["path"], "/getUpdates")
        self.assertEqual(captured["offset"], 8)
        self.assertEqual(captured["timeout"], 12)
        self.assertEqual(captured["allowed_updates"], ["message"])

    def test_long_text_is_split_below_telegram_limit(self):
        chunks = split_telegram_text(("word " * 2000).strip())

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 4000 for chunk in chunks))
        self.assertEqual(" ".join(chunks).split(), ("word " * 2000).split())

    async def test_default_client_preserves_bot_token_in_base_url(self):
        client = TelegramBotApiClient("test-token")
        try:
            request = client._client.build_request("POST", "getUpdates")
            self.assertEqual(request.url.path, "/bottest-token/getUpdates")
        finally:
            await client.close()

    async def test_network_error_suppresses_sensitive_http_exception_chain(self):
        async def handler(request):
            raise httpx.ConnectError(f"failed request: {request.url}")

        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = TelegramBotApiClient("test-token", http_client=http_client)
            with self.assertRaisesRegex(
                RuntimeError,
                "could not connect to api.telegram.org",
            ) as caught:
                await client.get_updates()

        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("test-token", str(caught.exception))

    async def test_http_401_explains_token_without_echoing_it(self):
        async def handler(request):
            return httpx.Response(401, json={"ok": False})

        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = TelegramBotApiClient("test-token", http_client=http_client)
            with self.assertRaisesRegex(
                RuntimeError,
                r"HTTP 401\. Check TELEGRAM_BOT_TOKEN",
            ) as caught:
                await client.get_updates()

        self.assertNotIn("test-token", str(caught.exception))

    async def test_http_409_explains_polling_conflict(self):
        async def handler(request):
            return httpx.Response(409, json={"ok": False})

        async with httpx.AsyncClient(
            base_url="https://example.test",
            transport=httpx.MockTransport(handler),
        ) as http_client:
            client = TelegramBotApiClient("test-token", http_client=http_client)
            with self.assertRaisesRegex(
                RuntimeError,
                r"HTTP 409\. Remove an existing webhook",
            ):
                await client.get_updates()


class FakeTelegramClient:
    def __init__(self, updates=None):
        self.updates = list(updates or [])
        self.sent = []
        self.documents = []
        self.actions = []
        self.get_updates_calls = []
        self.closed = False

    async def get_updates(self, *, offset=None, timeout=30):
        self.get_updates_calls.append({"offset": offset, "timeout": timeout})
        updates, self.updates = self.updates, []
        return updates

    async def send_message(self, chat_id, text):
        self.sent.append((chat_id, text))

    async def send_chat_action(self, chat_id, action="typing"):
        self.actions.append((chat_id, action))

    async def send_document(self, chat_id, path, *, caption=""):
        self.documents.append((chat_id, Path(path).name, caption))

    async def close(self):
        self.closed = True


class FakeBus:
    def __init__(self):
        self.subscribers = {}

    def subscribe_outbound(self, channel, handler):
        self.subscribers[channel] = handler


class FakeRuntime:
    def __init__(self):
        self.bus = FakeBus()
        self.submitted = []
        self.run_count = 0
        self.started = False
        self.stopped = False
        self.loop = None

    def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True

    async def submit_user_message(self, **kwargs):
        self.submitted.append(kwargs)

    async def run_once(self):
        self.run_count += 1


def private_update(update_id=1, *, chat_id=123, user_id=123, text="hello"):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id},
            "text": text,
        },
    }


class TelegramGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.old_cwd = Path.cwd()
        os.chdir(self.temp_dir.name)
        self.schema = temporary_postgres_schema("telegram_gateway")
        self.dsn = self.schema.__enter__()
        self.store = TelegramGatewayStore(self.dsn)
        self.client = FakeTelegramClient()
        self.runtime = FakeRuntime()
        self.gateway = TelegramGateway(
            runtime=self.runtime,
            client=self.client,
            identities=TelegramIdentityResolver(allowed_user_ids={123}),
            store=self.store,
            poll_timeout=10,
        )

    async def asyncTearDown(self):
        self.store.close()
        self.schema.__exit__(None, None, None)
        os.chdir(self.old_cwd)
        self.temp_dir.cleanup()

    async def test_private_text_enters_runtime_with_isolated_identity(self):
        await self.gateway.handle_update(private_update())

        self.assertEqual(self.client.actions, [(123, "typing")])
        self.assertEqual(self.runtime.run_count, 1)
        self.assertEqual(len(self.runtime.submitted), 1)
        submitted = self.runtime.submitted[0]
        self.assertEqual(submitted["channel"], "telegram")
        self.assertEqual(submitted["chat_id"], "tg_123_default_telegram_123")
        self.assertEqual(submitted["metadata"]["user_id"], "telegram_123")
        self.assertEqual(submitted["metadata"]["user_role"], "user")

    async def test_unapproved_user_gets_instruction_without_agent_call(self):
        await self.gateway.handle_update(private_update(user_id=999))

        self.assertEqual(self.runtime.run_count, 0)
        self.assertEqual(self.runtime.submitted, [])
        self.assertIn("999", self.client.sent[0][1])

    async def test_non_private_chat_is_ignored(self):
        update = private_update()
        update["message"]["chat"]["type"] = "group"

        await self.gateway.handle_update(update)

        self.assertEqual(self.runtime.run_count, 0)
        self.assertEqual(self.client.sent, [])

    async def test_new_command_rotates_active_conversation(self):
        self.assertEqual(self.store.get_conversation_id(123), "default")

        await self.gateway.handle_update(private_update(text="/new"))

        self.assertNotEqual(self.store.get_conversation_id(123), "default")
        self.assertEqual(self.runtime.run_count, 0)
        self.assertIn("新的对话", self.client.sent[0][1])

    async def test_poll_once_persists_next_offset(self):
        self.client.updates.append(private_update(update_id=41, text="/status"))

        await self.gateway.poll_once()

        self.assertEqual(self.store.get_offset(), 42)
        self.assertEqual(self.client.get_updates_calls[0]["timeout"], 10)

    async def test_outbound_message_routes_to_external_chat(self):
        await self.gateway.send(OutboundMessage(
            channel="telegram",
            chat_id="tg_123_default",
            content="reply",
        ))

        self.assertEqual(self.client.sent, [(123, "reply")])

    async def test_flush_outbox_sends_and_marks_message(self):
        message_id = self.store.enqueue_message(
            chat_id=123,
            text="scheduled report",
            source="scheduler",
        )

        await self.gateway.flush_outbox()

        self.assertEqual(self.client.sent, [("123", "scheduled report")])
        self.assertEqual(self.store.list_pending_messages(), [])
        self.assertEqual(message_id, 1)

    async def test_flush_outbox_sends_document_message(self):
        self.store.enqueue_document(
            chat_id=123,
            document_path="storage/reports/daily.md",
            caption="daily report",
            source="scheduler",
        )

        await self.gateway.flush_outbox()

        self.assertEqual(
            self.client.documents,
            [("123", "daily.md", "daily report")],
        )
        self.assertEqual(self.store.list_pending_messages(), [])

    async def test_files_command_lists_current_users_storage(self):
        storage = Path(".users/telegram_123/storage")
        (storage / "reports").mkdir(parents=True)
        (storage / "reports" / "daily.md").write_text("daily", encoding="utf-8")

        await self.gateway.handle_update(private_update(text="/files reports"))

        self.assertIn("daily.md", self.client.sent[-1][1])
        self.assertIn("/cat reports/daily.md", self.client.sent[-1][1])

    async def test_cat_command_previews_text_file(self):
        storage = Path(".users/telegram_123/storage")
        storage.mkdir(parents=True)
        (storage / "note.md").write_text("hello storage", encoding="utf-8")

        await self.gateway.handle_update(private_update(text="/cat note.md"))

        self.assertIn("hello storage", self.client.sent[-1][1])

    async def test_download_command_sends_document(self):
        storage = Path(".users/telegram_123/storage")
        storage.mkdir(parents=True)
        (storage / "note.md").write_text("hello storage", encoding="utf-8")

        await self.gateway.handle_update(private_update(text="/download note.md"))

        self.assertEqual(self.client.documents, [(123, "note.md", "storage/note.md")])

    async def test_storage_command_rejects_escape(self):
        await self.gateway.handle_update(private_update(text="/cat ../../.env"))

        self.assertIn("storage", self.client.sent[-1][1])


class AgentLoopIdentityTests(unittest.TestCase):
    def test_role_is_refreshed_on_each_trusted_inbound_message(self):
        session = Session(
            id="telegram:tg_123_default_admin",
            metadata={"user_id": "admin", "user_role": "admin"},
        )
        inbound = InboundMessage(
            channel="telegram",
            chat_id="tg_123_default_admin",
            sender="telegram:123",
            content="hello",
            metadata={"user_id": "admin", "user_role": "user"},
        )

        AgentLoop._apply_inbound_identity(None, session, inbound)

        self.assertEqual(session.metadata["user_role"], "user")

    def test_conflicting_user_identity_is_rejected(self):
        session = Session(
            id="telegram:tg_123_default_alice",
            metadata={"user_id": "alice", "user_role": "user"},
        )
        inbound = InboundMessage(
            channel="telegram",
            chat_id="tg_123_default_alice",
            sender="telegram:123",
            content="hello",
            metadata={"user_id": "bob", "user_role": "user"},
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            AgentLoop._apply_inbound_identity(None, session, inbound)


if __name__ == "__main__":
    unittest.main()
