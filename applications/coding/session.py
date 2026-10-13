from dataclasses import dataclass
from copy import deepcopy
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4

from config import WORKDIR
from runtime.sessions import Session, SessionManager
from runtime.context.events import ContextEvent, thaw


@dataclass
class TaskSessionRecord:
    session: Session
    task_id: str
    parent_session_id: str
    task_type: str
    task_root: Path


class TaskSessionFactory:
    def __init__(
        self,
        sessions: SessionManager,
        *,
        root: Path | None = None,
    ) -> None:
        self.sessions = sessions
        self.root = root or WORKDIR / ".coding_applications"
        self.root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        parent_session_id: str,
        task_type: str,
        user_request: str,
        original_request_ref: str = "",
        artifact_refs: list[dict] | None = None,
        user_id: str | None = None,
        user_role: str | None = None,
        task_id: str | None = None,
    ) -> TaskSessionRecord:
        task_id = task_id or f"{_slug(task_type)}-{uuid4().hex[:8]}"
        session_id = f"task:{task_id}"
        task_root = self.root / task_id
        task_root.mkdir(parents=True, exist_ok=True)
        session = self.sessions.get_or_create(session_id)
        session.active_agent = task_type
        session.metadata.update({
            "kind": "coding_application",
            "task_id": task_id,
            "task_type": task_type,
            "parent_session_id": parent_session_id,
            "status": "running",
            "user_request_summary": _request_summary(user_request),
            "original_request_ref": str(original_request_ref or ""),
            "artifact_refs": list(artifact_refs or []),
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
            task_root=task_root,
        )

    def load(self, task_id: str, *, parent_session_id: str, user_id: str | None) -> TaskSessionRecord:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", str(task_id)):
            raise ValueError("Invalid coding task_id")
        session_id = f"task:{task_id}"
        loader = getattr(self.sessions, "get_existing", None)
        if callable(loader):
            session = loader(session_id)
        else:
            cached = getattr(self.sessions, "sessions", getattr(self.sessions, "_sessions", {}))
            session = cached.get(session_id)
        if (session is None or session.metadata.get("kind") != "coding_application"
                or session.metadata.get("task_id") != task_id
                or session.metadata.get("parent_session_id") != parent_session_id
                or session.metadata.get("user_id") != user_id):
            raise ValueError("Coding task is unavailable for this parent session and user")
        return TaskSessionRecord(session, task_id, parent_session_id,
                                 str(session.metadata.get("task_type") or "coding"), self.root / task_id)

    def branch(self, source: TaskSessionRecord, *, task_id: str | None = None,
               workspace_metadata: dict | None = None) -> TaskSessionRecord:
        task_id = task_id or f"{_slug(source.task_type)}-{uuid4().hex[:8]}"
        session_id = f"task:{task_id}"
        original = source.session
        identifiers = {
            event.event_id: "evt_" + hashlib.sha256(f"{session_id}:{event.event_id}".encode()).hexdigest()[:32]
            for event in original.event_log
        }
        identifiers.update({
            item["snapshot_id"]: "snapshot_" + hashlib.sha256(f"{session_id}:{item['snapshot_id']}".encode()).hexdigest()[:32]
            for item in original.context_snapshots
        })
        identifiers.update({
            item["checkpoint_id"]: "ckpt_" + hashlib.sha256(f"{session_id}:{item['checkpoint_id']}".encode()).hexdigest()[:32]
            for item in original.checkpoints if item.get("checkpoint_id")
        })
        identifiers.update({original.id: session_id, source.task_id: task_id})
        metadata = _branch_copy(original.metadata, identifiers)
        for key in ("parent_run_id", "execution_budget", "run_state", "resource_state",
                    "verification", "task_reply", "task_log_path", "conclusions_path",
                    "native_response_context", "context_compaction_request"):
            metadata.pop(key, None)
        metadata.update(task_id=task_id, status="running", execution_status="branched",
                        branch_lineage={"task_id": source.task_id, "session_id": original.id,
                                        "snapshot_id": original.active_snapshot_id,
                                        "event_id_map": _branch_aliases(original, identifiers, "event_id_map",
                                                                        [event.event_id for event in original.event_log]),
                                        "snapshot_id_map": _branch_aliases(original, identifiers, "snapshot_id_map",
                                                                           [item["snapshot_id"] for item in original.context_snapshots])})
        if workspace_metadata is not None:
            metadata.update(deepcopy(workspace_metadata))
        session = Session(
            id=session_id, active_agent=original.active_agent,
            messages=_branch_copy(original.messages, identifiers), metadata=metadata,
            event_log=[ContextEvent.from_dict(_branch_copy(event.to_dict(), identifiers)) for event in original.event_log],
            archive_boundary_seq=original.archive_boundary_seq,
            checkpoints=_branch_copy(original.checkpoints, identifiers),
            context_snapshots=_branch_copy(original.context_snapshots, identifiers),
            active_snapshot_id=identifiers.get(original.active_snapshot_id, ""),
        )
        # The inherited snapshots describe local cloned events, so their source
        # hashes must describe that new identity too (raw content stays exact).
        from runtime.context.snapshots import _source_hash
        for snapshot in session.context_snapshots:
            covered = [event for event in session.event_log
                       if snapshot["covered_event_start_seq"] <= event.seq <= snapshot["covered_event_end_seq"]]
            source_hash = _source_hash(covered, snapshot["source_task_state_version"])
            if snapshot.get("previous_snapshot_id"):
                source_hash = hashlib.sha256(
                    f"{source_hash}:{snapshot['previous_snapshot_id']}".encode()
                ).hexdigest()
            snapshot["source_hash"] = source_hash
        # Validity must be durable in the first branch transaction: a crash
        # before the runner resumes must never expose inherited proof as fresh.
        from .task_lifecycle import resume_task_state
        resume_task_state(session, branched=True)
        # Recovery from a checkpoint must apply the same branch boundary even
        # if the live metadata is later unavailable or corrupted.
        for checkpoint in session.checkpoints:
            payload = checkpoint.get("state")
            if isinstance(payload, dict) and isinstance(payload.get("task_state"), dict):
                holder = SimpleNamespace(metadata={"task_id": task_id, "task_state": payload["task_state"]})
                resume_task_state(holder, branched=True)
                payload["task_state"] = holder.metadata["task_state"]
        task_root = self.root / task_id
        task_root.mkdir(parents=True, exist_ok=True)
        saver = getattr(self.sessions, "save_branch", None)
        if callable(saver):
            saver(session)
        else:
            self.sessions.save(session)
        return TaskSessionRecord(session, task_id, source.parent_session_id, source.task_type, task_root)


def _branch_aliases(original, identifiers, field_name, local_ids):
    """Keep raw-source recovery URLs usable through multiple local branches."""
    aliases = {identifier: identifiers[identifier] for identifier in local_ids}
    lineage = original.metadata.get("branch_lineage")
    inherited = lineage.get(field_name) if isinstance(lineage, dict) else None
    if isinstance(inherited, dict):
        for alias, target in inherited.items():
            if isinstance(alias, str) and isinstance(target, str) and target in aliases:
                aliases.setdefault(alias, identifiers[target])
    return aliases


def _branch_copy(value, identifiers, *, key=""):
    """Rekey structured identity; raw message contents remain exact source data."""
    if key == "content":
        return deepcopy(value)
    if isinstance(value, dict):
        return {identifiers.get(name, name): _branch_copy(item, identifiers, key=name)
                for name, item in value.items() if name != "payload_sha256"}
    if isinstance(value, (list, tuple)):
        return [_branch_copy(item, identifiers, key=key) for item in value]
    if isinstance(value, str) and key != "content":
        if value in identifiers:
            return identifiers[value]
        for old, new in identifiers.items():
            value = value.replace(f"event://{old}", f"event://{new}")
            value = value.replace(f"snapshot://{old}", f"snapshot://{new}")
        return value
    return deepcopy(value)


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower())
    return slug.strip("-") or "task"


def _request_summary(value: str, limit: int = 300) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 15)].rstrip() + "...[truncated]"
