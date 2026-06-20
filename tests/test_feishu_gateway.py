import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bus.events import OutboundMessage
from gateway.feishu.adapter import FeishuGateway
from gateway.feishu.identity import FeishuIdentity, FeishuIdentityResolver
from gateway.feishu.store import FeishuGatewayStore
from postgres_utils import temporary_postgres_schema


class FakeBus:
    def __init__(self):
        self.handlers = {}

    def subscribe_outbound(self, channel, handler):
        self.handlers[channel] = handler


class FakeRuntime:
    def __init__(self):
        self.bus = FakeBus()
        self.submitted = []
        self.run_count = 0
        self.started = False
        self.loop = SimpleNamespace(sessions=SimpleNamespace(close=lambda: None))

    def start(self):
        self.started = True

    async def stop(self):
        pass

    async def submit_user_message(self, **kwargs):
        self.submitted.append(kwargs)

    async def run_once(self):
        self.run_count += 1


class FakeFeishuClient:
    def __init__(self):
        self.messages = []
        self.documents = []
        self.closed = False

    async def send_message(self, receive_id, text, *, receive_id_type="chat_id"):
        self.messages.append({
            "receive_id": receive_id,
            "text": text,
            "receive_id_type": receive_id_type,
        })

    async def send_document(self, chat_id, path, *, caption=""):
        self.documents.append({
            "chat_id": chat_id,
            "path": str(path),
            "caption": caption,
        })

    async def close(self):
        self.closed = True


class FeishuIdentityResolverTests(unittest.TestCase):
    def test_environment_user_map_binds_admin(self):
        with patch.dict(
            "os.environ",
            {
                "FEISHU_USER_MAP": json.dumps({
                    "ou_admin": {"user_id": "admin", "role": "admin"}
                }),
            },
            clear=True,
        ):
            identity = FeishuIdentityResolver.from_env().resolve("ou_admin")

        self.assertEqual("admin", identity.user_id)
        self.assertEqual("admin", identity.role)

    def test_wildcard_maps_to_isolated_regular_user(self):
        resolver = FeishuIdentityResolver(allow_any=True)

        identity = resolver.resolve("ou_abc")

        self.assertEqual("feishu_ou_abc", identity.user_id)
        self.assertEqual("user", identity.role)


class FeishuGatewayStoreTests(unittest.TestCase):
    def test_event_dedupe_conversation_and_outbox_round_trip(self):
        with temporary_postgres_schema("feishu_store") as dsn:
            store = FeishuGatewayStore(dsn)
            try:
                self.assertTrue(store.mark_event_seen("evt_1"))
                self.assertFalse(store.mark_event_seen("evt_1"))

                runtime_id = store.runtime_chat_id("oc_chat", user_id="admin")
                self.assertTrue(runtime_id.startswith("fs:"))

                store.enqueue_message(chat_id="oc_chat", text="hello")
                store.enqueue_document(
                    chat_id="oc_chat",
                    document_path="storage/reports/a.md",
                    caption="report",
                )
                pending = store.list_pending_messages()
                self.assertEqual(["text", "document"], [item["message_type"] for item in pending])
                self.assertEqual("storage/reports/a.md", pending[1]["document_path"])
            finally:
                store.close()


class FeishuGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.schema = temporary_postgres_schema("feishu_gateway")
        self.dsn = self.schema.__enter__()
        self.store = FeishuGatewayStore(self.dsn)
        self.runtime = FakeRuntime()
        self.client = FakeFeishuClient()
        self.gateway = FeishuGateway(
            runtime=self.runtime,
            client=self.client,
            identities=FeishuIdentityResolver(
                user_map={"ou_user": FeishuIdentity(user_id="admin", role="admin")}
            ),
            store=self.store,
            verification_token="verify-token",
        )

    async def asyncTearDown(self):
        self.store.close()
        self.schema.__exit__(None, None, None)
        self.temp_dir.cleanup()

    async def test_url_verification_returns_challenge(self):
        response = await self.gateway.handle_callback({
            "type": "url_verification",
            "token": "verify-token",
            "challenge": "abc123",
        })

        self.assertEqual({"challenge": "abc123"}, response.payload)

    async def test_private_text_enters_runtime_with_isolated_identity(self):
        response = await self.gateway.handle_callback(_message_event("hello"))
        await asyncio.sleep(0.05)

        self.assertEqual({"ok": True}, response.payload)
        self.assertEqual(1, self.runtime.run_count)
        self.assertEqual("hello", self.runtime.submitted[0]["content"])
        self.assertEqual("feishu", self.runtime.submitted[0]["channel"])
        self.assertEqual("admin", self.runtime.submitted[0]["metadata"]["user_id"])
        self.assertEqual("admin", self.runtime.submitted[0]["metadata"]["user_role"])

    async def test_duplicate_event_is_not_processed_twice(self):
        await self.gateway.handle_callback(_message_event("hello", event_id="evt_dup"))
        await self.gateway.handle_callback(_message_event("hello again", event_id="evt_dup"))
        await asyncio.sleep(0.05)

        self.assertEqual(1, self.runtime.run_count)
        self.assertEqual("hello", self.runtime.submitted[0]["content"])

    async def test_unapproved_user_gets_instruction_without_agent_call(self):
        await self.gateway.handle_callback(_message_event("hello", open_id="ou_unknown"))
        await asyncio.sleep(0.05)

        self.assertEqual(0, self.runtime.run_count)
        self.assertIn("尚未授权", self.client.messages[0]["text"])

    async def test_status_command_replies_with_chat_id(self):
        await self.gateway.handle_callback(_message_event("/status"))
        await asyncio.sleep(0.05)

        self.assertEqual(0, self.runtime.run_count)
        self.assertIn("飞书 chat_id：oc_chat", self.client.messages[0]["text"])

    async def test_outbound_message_routes_to_feishu_chat(self):
        runtime_id = self.store.runtime_chat_id("oc_chat", user_id="admin")

        await self.gateway.send(OutboundMessage(
            channel="feishu",
            chat_id=runtime_id,
            content="reply",
        ))

        self.assertEqual("oc_chat", self.client.messages[0]["receive_id"])
        self.assertEqual("reply", self.client.messages[0]["text"])

    async def test_flush_outbox_sends_document_message(self):
        report = Path(self.temp_dir.name) / "report.md"
        report.write_text("report", encoding="utf-8")
        self.store.enqueue_document(
            chat_id="oc_chat",
            document_path=report,
            caption="daily",
        )

        await self.gateway.flush_outbox()

        self.assertEqual("oc_chat", self.client.documents[0]["chat_id"])
        self.assertEqual("daily", self.client.documents[0]["caption"])
        self.assertFalse(self.store.list_pending_messages())


def _message_event(
    text: str,
    *,
    event_id: str = "evt_1",
    chat_id: str = "oc_chat",
    open_id: str = "ou_user",
    chat_type: str = "p2p",
) -> dict:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": "im.message.receive_v1",
            "token": "verify-token",
        },
        "event": {
            "sender": {
                "sender_id": {
                    "open_id": open_id,
                },
            },
            "message": {
                "message_id": "om_1",
                "chat_id": chat_id,
                "chat_type": chat_type,
                "message_type": "text",
                "content": json.dumps({"text": text}, ensure_ascii=False),
            },
        },
    }


if __name__ == "__main__":
    unittest.main()
