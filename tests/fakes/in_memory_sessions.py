from __future__ import annotations

from collections import OrderedDict

from runtime.sessions import Session


class InMemorySessionManager:
    """SessionManager-compatible test double with no external persistence."""

    def __init__(self, *, max_sessions: int = 128) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._sessions: OrderedDict[str, Session] = OrderedDict()
        self.saved_ids: list[str] = []
        self.closed = False

    def get_or_create(self, session_id: str) -> Session:
        session = self._sessions.get(session_id)
        if session is None:
            session = Session(id=session_id)
            self._sessions[session_id] = session
            self._evict_if_needed()
        else:
            self._sessions.move_to_end(session_id)
        return session

    def save(self, session: Session) -> None:
        session.touch()
        self._sessions[session.id] = session
        self._sessions.move_to_end(session.id)
        self.saved_ids.append(session.id)
        self._evict_if_needed()

    def list_sessions(self) -> list[dict]:
        return [
            {
                "id": session.id,
                "active_agent": session.active_agent,
                "created_at": session.created_at,
                "updated_at": session.updated_at,
                "last_compacted": session.last_compacted,
                "metadata": dict(session.metadata),
            }
            for session in reversed(self._sessions.values())
        ]

    def delete(self, session_id: str) -> bool:
        return self._sessions.pop(session_id, None) is not None

    def close(self) -> None:
        self.closed = True

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
