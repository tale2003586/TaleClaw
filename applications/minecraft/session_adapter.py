from __future__ import annotations


class MinecraftSessionAdapter:
    def __init__(self, sessions) -> None:
        self.sessions = sessions

    def bind_task(self, session_id: str, task_id: str) -> None:
        session = self.sessions.get_or_create(session_id)
        session.metadata["minecraft_task_id"] = task_id
        self.sessions.save(session)

    def task_id(self, session_id: str) -> str | None:
        session = self.sessions.get_or_create(session_id)
        value = session.metadata.get("minecraft_task_id")
        return str(value) if value else None
