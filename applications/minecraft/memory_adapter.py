from __future__ import annotations


class MinecraftMemoryAdapter:
    ALLOWED_KINDS = {
        "world_location",
        "task_history",
        "failed_strategy",
        "successful_plan",
        "skill_experience",
    }

    def __init__(self, memory_store, *, max_chars: int = 2000) -> None:
        self.memory_store = memory_store
        self.max_chars = max(100, int(max_chars))
        self.read_count = 0

    def retrieve(
        self,
        query: str,
        *,
        purpose: str,
        budget_chars: int | None = None,
    ) -> str:
        if purpose not in {
            "initial_plan",
            "global_replan",
            "llm_critic",
            "important_checkpoint",
        }:
            return ""
        self.read_count += 1
        store = self.memory_store
        if hasattr(store, "recall"):
            value = store.recall(query)
        elif hasattr(store, "read_prompt_memory"):
            value = store.read_prompt_memory()
        elif hasattr(store, "read_all"):
            value = store.read_all()
        else:
            value = ""
        limit = min(self.max_chars, int(budget_chars or self.max_chars))
        return str(value or "")[:limit]

    def remember(self, kind: str, content: str, *, source_ref: str = "") -> str:
        if kind not in self.ALLOWED_KINDS:
            raise ValueError(f"unsupported Minecraft memory kind: {kind}")
        text = f"[minecraft:{kind}] {content}"
        if source_ref:
            text += f" (source={source_ref})"
        store = self.memory_store
        if hasattr(store, "append_pending"):
            return str(store.append_pending(text, tag=f"minecraft_{kind}", source_ref=source_ref))
        if hasattr(store, "append"):
            return str(store.append("memory", text))
        raise RuntimeError("memory store is not writable")
