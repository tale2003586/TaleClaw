from __future__ import annotations

from abc import ABC, abstractmethod

from bus.events import OutboundMessage


class ChannelAdapter(ABC):
    """Connects an external chat channel to the internal message bus."""

    channel: str

    @abstractmethod
    async def run_forever(self) -> None:
        """Consume external messages until the adapter is stopped."""

    @abstractmethod
    async def send(self, message: OutboundMessage) -> None:
        """Deliver one internal outbound message to the external channel."""

    @abstractmethod
    async def close(self) -> None:
        """Release adapter resources."""
