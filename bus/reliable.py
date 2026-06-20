from __future__ import annotations

import threading
from typing import Any

from .protocol import AgentMessage, MessageType


class ReliableMessageBus:
    """Request-response correlation layer over the existing JSONL team bus."""

    def __init__(self, base_bus) -> None:
        self._bus = base_bus
        self._pending: dict[str, threading.Event] = {}
        self._responses: dict[str, AgentMessage] = {}
        self._lock = threading.Lock()

    def request(
        self,
        sender: str,
        to: str,
        msg_type: MessageType,
        payload: dict[str, Any],
        *,
        timeout: float = 60,
    ) -> AgentMessage | None:
        message = AgentMessage(
            sender=sender,
            recipient=to,
            type=msg_type,
            payload=payload,
        )
        event = threading.Event()
        with self._lock:
            self._pending[message.id] = event
        self.send(message)
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(message.id, None)
            return None
        with self._lock:
            return self._responses.pop(message.id, None)

    def respond(
        self,
        original: AgentMessage,
        msg_type: MessageType,
        payload: dict[str, Any],
        *,
        sender: str | None = None,
    ) -> AgentMessage:
        response = AgentMessage(
            sender=sender or original.recipient,
            recipient=original.sender,
            type=msg_type,
            correlation_id=original.id,
            payload=payload,
        )
        self.send(response)
        return response

    def send(self, message: AgentMessage) -> str:
        return self._bus.send(
            message.sender,
            message.recipient,
            message.to_json(),
            message.type.value,
            {
                "id": message.id,
                "recipient": message.recipient,
                "correlation_id": message.correlation_id,
                "payload": message.payload,
                "ttl_seconds": message.ttl_seconds,
            },
        )

    def notify_arrival(self, raw_message: dict[str, Any] | AgentMessage) -> AgentMessage | None:
        message = (
            raw_message
            if isinstance(raw_message, AgentMessage)
            else AgentMessage.from_json(raw_message)
        )
        if not message.correlation_id:
            return message
        with self._lock:
            self._responses[message.correlation_id] = message
            event = self._pending.pop(message.correlation_id, None)
        if event is not None:
            event.set()
        return message
