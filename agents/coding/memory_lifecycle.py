from memory.lifecycle import MemoryLifecycle


class TaskMemoryLifecycle(MemoryLifecycle):
    """Keep task history local without promoting wrapped task prompts as memories."""

    def _extract_explicit_memory(self, text: str) -> str:
        return ""

    def _candidate_memory_enabled(self) -> bool:
        return False
