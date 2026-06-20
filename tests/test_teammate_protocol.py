from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from bus import AgentMessage, MessageType
from coding_runtime.protocols import ProtocolManager
from coding_runtime.teammate import TeammateManager
from tools.handlers import _spawn_teammate_with_protocol


class FakeTeam:
    def __init__(self) -> None:
        self.spawn_calls = []

    def spawn(self, name, role, prompt):
        self.spawn_calls.append((name, role, prompt))
        return f"spawned {name}"


class FakeBus:
    def __init__(self) -> None:
        self.sent = []

    def send(self, sender, to, content, msg_type="message", extra=None):
        message = {
            "sender": sender,
            "recipient": to,
            "content": content,
            "type": msg_type,
        }
        if extra:
            message.update(extra)
        self.sent.append(message)
        return "sent"


class FakeReliableBus:
    def __init__(self) -> None:
        self.sent = []

    def send(self, message):
        self.sent.append(message)
        return "sent"

    def notify_arrival(self, raw_message):
        return raw_message


class TeammateProtocolTests(unittest.TestCase):
    def test_spawn_teammate_uses_structured_task_assign_message(self) -> None:
        team = FakeTeam()

        output = _spawn_teammate_with_protocol(
            team,
            "alice",
            "researcher",
            "Inspect auth flow.",
        )

        self.assertEqual("spawned alice", output)
        name, role, prompt = team.spawn_calls[0]
        message = AgentMessage.from_json(prompt)
        self.assertEqual(("alice", "researcher"), (name, role))
        self.assertEqual(MessageType.TASK_ASSIGN, message.type)
        self.assertEqual("lead", message.sender)
        self.assertEqual("alice", message.recipient)
        self.assertIn("Inspect auth flow.", message.payload["prompt"])

    def test_teammate_error_is_sent_to_lead_as_protocol_error(self) -> None:
        fake_bus = FakeBus()
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = TeammateManager(Path(tmpdir) / ".team")
            with patch("coding_runtime.teammate.BUS", fake_bus):
                manager._send_error_to_lead("alice", RuntimeError("boom"))

        self.assertEqual(1, len(fake_bus.sent))
        message = AgentMessage.from_json(fake_bus.sent[0])
        self.assertEqual(MessageType.ERROR, message.type)
        self.assertEqual("alice", message.sender)
        self.assertEqual("lead", message.recipient)
        self.assertIn("RuntimeError: boom", message.payload["error"])

    def test_plan_request_and_review_use_correlated_protocol_messages(self) -> None:
        fake_reliable = FakeReliableBus()
        manager = ProtocolManager()

        with patch("coding_runtime.protocols.RELIABLE_BUS", fake_reliable):
            request_output = manager.handle_plan_request("alice", "1. Inspect\n2. Patch")
            request_id = next(iter(manager.plan_requests))
            review_output = manager.handle_plan_review(request_id, True, "go ahead")

        self.assertIn(request_id, request_output)
        self.assertIn("approved", review_output)
        request, response = fake_reliable.sent
        self.assertEqual(MessageType.PLAN_REQUEST, request.type)
        self.assertEqual(MessageType.PLAN_RESPONSE, response.type)
        self.assertEqual(request_id, response.correlation_id)
        self.assertEqual("alice", response.recipient)
        self.assertTrue(response.payload["approve"])


if __name__ == "__main__":
    unittest.main()
