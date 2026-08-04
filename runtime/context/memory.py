"""Durable memory context rendering."""

from __future__ import annotations


class ContextMemoryService:
    def __init__(
        self,
        *,
        memory_store=None,
        semantic_memory_retriever=None,
    ) -> None:
        self.memory_store = memory_store
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
        if self.memory_store is None:
            return ""

        store = self.memory_store
        if hasattr(store, "for_session"):
            store = store.for_session(session)
        if current_request.strip() and hasattr(store, "recall"):
            text = store.recall(current_request).strip()
        else:
            text = store.read_all().strip()
        if not text or text == "No relevant memory found.":
            return ""
        return "<memory>\n" + text + "\n</memory>"

def _queue_trace_events(session, service) -> None:
    if not hasattr(service, "drain_trace_events"):
        return
    events = service.drain_trace_events()
    metadata = getattr(session, "metadata", None)
    if events and isinstance(metadata, dict):
        metadata.setdefault("memory_trace_events", []).extend(events)
