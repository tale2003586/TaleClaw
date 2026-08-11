"""Durable memory context rendering."""

from __future__ import annotations


class ContextMemoryService:
    def __init__(
        self,
        *,
        semantic_memory_retriever=None,
    ) -> None:
        self.semantic_memory_retriever = semantic_memory_retriever

    def build_memory_block(self, session, *, current_request: str = "") -> str:
        if self.semantic_memory_retriever is not None:
            from memory.commands import MemoryContext

            result = self.semantic_memory_retriever.retrieve(
                current_request,
                MemoryContext.from_session(session),
            )
            _queue_trace_events(session, self.semantic_memory_retriever)
            return self.semantic_memory_retriever.render(result)
        return ""

def _queue_trace_events(session, service) -> None:
    if not hasattr(service, "drain_trace_events"):
        return
    events = service.drain_trace_events()
    metadata = getattr(session, "metadata", None)
    if events and isinstance(metadata, dict):
        metadata.setdefault("memory_trace_events", []).extend(events)
