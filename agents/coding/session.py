from dataclasses import dataclass
from pathlib import Path
import re
from uuid import uuid4

from config import WORKDIR
from sessions import Session, SessionManager


@dataclass
class TaskSessionRecord:
    session: Session
    task_id: str
    parent_session_id: str
    task_type: str
    memory_root: Path


class TaskSessionFactory:
    def __init__(
        self,
        sessions: SessionManager,
        *,
        root: Path | None = None,
    ) -> None:
        self.sessions = sessions
        self.root = root or WORKDIR / ".task_sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        parent_session_id: str,
        task_type: str,
        user_request: str,
        user_id: str | None = None,
        user_role: str | None = None,
    ) -> TaskSessionRecord:
        task_id = f"{_slug(task_type)}-{uuid4().hex[:8]}"
        session_id = f"task:{task_id}"
        memory_root = self.root / task_id / "memory"
        memory_root.mkdir(parents=True, exist_ok=True)
        resolved_memory_root = memory_root.resolve()
        try:
            stored_memory_root = str(resolved_memory_root.relative_to(WORKDIR.resolve()))
        except ValueError:
            stored_memory_root = str(resolved_memory_root)
        session = self.sessions.get_or_create(session_id)
        session.current_mode = task_type
        session.metadata.update({
            "kind": "task_session",
            "task_id": task_id,
            "task_type": task_type,
            "parent_session_id": parent_session_id,
            "status": "running",
            "user_request": user_request,
            "memory_root": stored_memory_root,
        })
        if user_id:
            session.metadata["user_id"] = user_id
        if user_role:
            session.metadata["user_role"] = user_role
        return TaskSessionRecord(
            session=session,
            task_id=task_id,
            parent_session_id=parent_session_id,
            task_type=task_type,
            memory_root=memory_root,
        )


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    return slug.strip("-") or "task"
