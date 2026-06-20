from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import json
import time
import uuid
from typing import Any


class MessageType(str, Enum):
    TASK_ASSIGN = "task_assign"
    TASK_RESULT = "task_result"
    TASK_PROGRESS = "task_progress"
    QUERY = "query"
    RESPONSE = "response"
    PLAN_REQUEST = "plan_request"
    PLAN_RESPONSE = "plan_response"
    SHUTDOWN_REQUEST = "shutdown_request"
    SHUTDOWN_RESPONSE = "shutdown_response"
    ERROR = "error"
    BROADCAST = "broadcast"
    MESSAGE = "message"


@dataclass
class AgentMessage:
    sender: str
    recipient: str
    type: MessageType
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    correlation_id: str | None = None
    timestamp: float = field(default_factory=time.time)
    ttl_seconds: int = 300

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "sender": self.sender,
            "recipient": self.recipient,
            "type": self.type.value,
            "correlation_id": self.correlation_id,
            "payload": self.payload,
            "timestamp": self.timestamp,
            "ttl_seconds": self.ttl_seconds,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)

    @classmethod
    def from_json(cls, data: str | dict[str, Any]) -> "AgentMessage":
        if isinstance(data, str):
            data = json.loads(data)
        if not isinstance(data, dict):
            raise ValueError("AgentMessage data must be a JSON object.")

        if _looks_like_bus_wrapper(data):
            return cls.from_json(_unwrap_bus_wrapper(data))

        msg_type = _message_type(data.get("type"))
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
            if "content" in data:
                payload["content"] = data.get("content")
            for key in (
                "request_id",
                "approve",
                "approved",
                "feedback",
                "plan",
                "task_id",
                "summary",
                "status",
            ):
                if key in data:
                    payload[key] = data.get(key)

        return cls(
            id=str(data.get("id") or data.get("request_id") or uuid.uuid4().hex[:8]),
            sender=str(data.get("sender") or ""),
            recipient=str(data.get("recipient") or data.get("to") or ""),
            type=msg_type,
            correlation_id=(
                str(data.get("correlation_id"))
                if data.get("correlation_id") is not None
                else None
            ),
            payload=payload,
            timestamp=float(data.get("timestamp") or time.time()),
            ttl_seconds=int(data.get("ttl_seconds") or 300),
        )


def _message_type(value: Any) -> MessageType:
    raw = str(value or "message")
    aliases = {
        "plan_approval_request": MessageType.PLAN_REQUEST,
        "plan_approval_response": MessageType.PLAN_RESPONSE,
    }
    if raw in aliases:
        return aliases[raw]
    try:
        return MessageType(raw)
    except ValueError:
        return MessageType.MESSAGE


def render_agent_message(message: AgentMessage | dict[str, Any] | str) -> str:
    """Render a structured bus message into model-readable text."""
    if not isinstance(message, AgentMessage):
        message = AgentMessage.from_json(message)

    header = [
        "<agent_message",
        f'id="{message.id}"',
        f'type="{message.type.value}"',
        f'from="{message.sender}"',
    ]
    if message.recipient:
        header.append(f'to="{message.recipient}"')
    if message.correlation_id:
        header.append(f'correlation_id="{message.correlation_id}"')
    lines = [" ".join(header) + ">"]
    payload = message.payload or {}

    if message.type == MessageType.TASK_ASSIGN:
        lines.append(f"Task: {payload.get('description') or payload.get('content') or ''}")
        if payload.get("prompt"):
            lines.append(str(payload["prompt"]))
    elif message.type == MessageType.TASK_RESULT:
        lines.append(f"Status: {payload.get('status', '')}")
        lines.append(str(payload.get("summary", payload.get("content", ""))))
    elif message.type == MessageType.TASK_PROGRESS:
        lines.append(str(payload.get("progress", payload.get("content", ""))))
    elif message.type == MessageType.PLAN_REQUEST:
        lines.append(f"Plan review request: {message.id}")
        lines.append(str(payload.get("plan", payload.get("content", ""))))
    elif message.type == MessageType.PLAN_RESPONSE:
        lines.append(f"Approved: {payload.get('approve', payload.get('approved', ''))}")
        if payload.get("feedback"):
            lines.append(str(payload["feedback"]))
    elif message.type == MessageType.SHUTDOWN_REQUEST:
        lines.append(str(payload.get("content", "Please shut down gracefully.")))
    elif message.type == MessageType.SHUTDOWN_RESPONSE:
        lines.append(f"Approved: {payload.get('approve', payload.get('approved', ''))}")
        if payload.get("details"):
            lines.append(str(payload["details"]))
    elif message.type == MessageType.ERROR:
        lines.append(f"Error: {payload.get('error', payload.get('content', ''))}")
        if payload.get("traceback"):
            lines.append(str(payload["traceback"]))
    elif message.type == MessageType.BROADCAST:
        lines.append(str(payload.get("content", "")))
    else:
        lines.append(str(payload.get("content", payload)))

    lines.append("</agent_message>")
    return "\n".join(line for line in lines if line is not None)


def _looks_like_bus_wrapper(data: dict[str, Any]) -> bool:
    if "payload" in data:
        return False
    content = data.get("content")
    if not isinstance(content, str):
        return False
    stripped = content.strip()
    return stripped.startswith("{") and stripped.endswith("}")


def _unwrap_bus_wrapper(data: dict[str, Any]) -> dict[str, Any]:
    try:
        inner = json.loads(str(data.get("content") or ""))
    except json.JSONDecodeError:
        return data
    if not isinstance(inner, dict):
        return data
    inner.setdefault("sender", data.get("sender", ""))
    inner.setdefault("timestamp", data.get("timestamp", time.time()))
    inner.setdefault("type", data.get("type", "message"))
    return inner
