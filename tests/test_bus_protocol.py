import threading
import time
import unittest

from bus import AgentMessage, MessageType, ReliableMessageBus, render_agent_message


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
        return "ok"


class BusProtocolTests(unittest.TestCase):
    def test_agent_message_round_trips_through_jsonl_wrapper(self) -> None:
        message = AgentMessage(
            sender="lead",
            recipient="worker",
            type=MessageType.TASK_ASSIGN,
            payload={"description": "inspect auth", "prompt": "read auth files"},
        )
        wrapper = {
            "sender": "lead",
            "content": message.to_json(),
            "type": "task_assign",
            "timestamp": message.timestamp,
        }

        parsed = AgentMessage.from_json(wrapper)
        rendered = render_agent_message(parsed)

        self.assertEqual(message.id, parsed.id)
        self.assertEqual(MessageType.TASK_ASSIGN, parsed.type)
        self.assertIn("inspect auth", rendered)
        self.assertIn("read auth files", rendered)

    def test_reliable_bus_correlates_response(self) -> None:
        base_bus = FakeBus()
        reliable = ReliableMessageBus(base_bus)

        def respond_later() -> None:
            while not base_bus.sent:
                time.sleep(0.01)
            original = AgentMessage.from_json(base_bus.sent[0])
            reliable.notify_arrival(AgentMessage(
                sender="worker",
                recipient="lead",
                type=MessageType.RESPONSE,
                correlation_id=original.id,
                payload={"ok": True},
            ))

        thread = threading.Thread(target=respond_later)
        thread.start()
        response = reliable.request(
            "lead",
            "worker",
            MessageType.QUERY,
            {"question": "ready?"},
            timeout=1,
        )
        thread.join(timeout=1)

        self.assertIsNotNone(response)
        self.assertEqual({"ok": True}, response.payload)

    def test_reliable_bus_request_times_out(self) -> None:
        reliable = ReliableMessageBus(FakeBus())

        response = reliable.request(
            "lead",
            "worker",
            MessageType.QUERY,
            {"question": "ready?"},
            timeout=0.01,
        )

        self.assertIsNone(response)

    def test_old_format_message_falls_back_to_generic_payload(self) -> None:
        message = AgentMessage.from_json({
            "sender": "alice",
            "content": "plain hello",
            "type": "message",
        })

        self.assertEqual(MessageType.MESSAGE, message.type)
        self.assertEqual("alice", message.sender)
        self.assertEqual({"content": "plain hello"}, message.payload)


if __name__ == "__main__":
    unittest.main()
