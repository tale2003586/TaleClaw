"""Durable and working-memory context rendering."""

from __future__ import annotations


class ContextMemoryService:
    def __init__(
        self,
        *,
        memory_store=None,
        working_memory_renderer=None,
        semantic_memory_retriever=None,
    ) -> None:
        self.memory_store = memory_store
        self.working_memory_renderer = working_memory_renderer
        self.semantic_memory_retriever = semantic_memory_retriever

    def build_memory_block(self, session, *, current_request: str = "") -> str:
        if self.semantic_memory_retriever is not None:
            from memory.commands import MemoryContext

            result = self.semantic_memory_retriever.retrieve(
                current_request,
                MemoryContext.from_session(session),
            )
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

    def build_working_memory_block(self, session) -> str:
        if self.working_memory_renderer is None:
            return ""
        return self.working_memory_renderer(session)
