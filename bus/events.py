# bus/events.py

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _empty_media() -> list[str]:
    return []


def _empty_metadata() -> dict[str, Any]:
    return {}


@dataclass
class InboundMessage:
    channel: str
    chat_id: str
    sender: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=_empty_media)
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)

    @property
    def session_key(self) -> str:
        return f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    channel: str
    chat_id: str
    content: str
    thinking: str | None = None
    reply_to: str | None = None
    media: list[str] = field(default_factory=_empty_media)
    metadata: dict[str, Any] = field(default_factory=_empty_metadata)
