from __future__ import annotations

from collections import OrderedDict

from runtime.sessions.session import Session


class EvaluationSessionManager:
    """Ephemeral SessionManager used only by deterministic scripted evaluations."""

    def __init__(self, *, max_sessions: int = 128) -> None:
        self.max_sessions = max(1, int(max_sessions))
        self._sessions: OrderedDict[str, Session] = OrderedDict()

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
        self._evict_if_needed()

    def close(self) -> None:
        self._sessions.clear()

    def _evict_if_needed(self) -> None:
        while len(self._sessions) > self.max_sessions:
            self._sessions.popitem(last=False)
