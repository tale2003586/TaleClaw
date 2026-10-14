"""Authenticated console actions over existing memory tables, without runtime bootstrapping."""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from memory.commands import MemoryTransition
from memory.domain import MemoryStatus
from memory.postgres_repository import PostgresMemoryRepository
from memory.repository import InvalidTransition, MemoryNotFound, ScopeDenied, VersionConflict
from user_scope import normalize_user_id
from web.memory_view import (
    _authorized_owners, _bound_task_owners, _evidence, _item, _mutable_owners,
    load_memory_session,
)


class MemoryStorageUnavailable(RuntimeError):
    """Safe client message when existing memory storage cannot complete an action."""


def apply_memory_action(user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Apply one explicit action with the browser's version inside the DB lock.

    Owner identity is resolved from the authenticated account and stored Web
    session. Neither owner IDs nor scopes supplied by the browser are trusted.
    """
    user_id = normalize_user_id(user_id)
    if not isinstance(payload, dict):
        raise ValueError("Memory action payload must be an object.")
    if set(payload) - {"id", "expected_version", "action", "session_id", "reason"}:
        raise ValueError("Unknown memory action fields.")
    memory_id = payload.get("id")
    version = payload.get("expected_version")
    action = payload.get("action")
    reason = payload.get("reason", "")
    session_id = payload.get("session_id", "")
    if not isinstance(memory_id, str) or not memory_id.strip() or len(memory_id) > 200 or "\x00" in memory_id:
        raise ValueError("A valid memory ID is required.")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1 or version > 2_147_483_647:
        raise ValueError("expected_version must be a positive integer.")
    if not isinstance(action, str) or action not in {"confirm", "reject", "revoke"}:
        raise ValueError("Invalid memory action.")
    if not isinstance(reason, str) or len(reason) > 1000 or "\x00" in reason:
        raise ValueError("Memory action reason must be a string of at most 1000 characters.")
    if not isinstance(session_id, str):
        raise ValueError("session_id must be a string.")
    session = load_memory_session(session_id, user_id) if session_id else None
    _authorized_owners(user_id, session)
    task_owners, _ = _bound_task_owners(user_id, session)
    owners = _mutable_owners(user_id, task_owners)
    statuses = {
        "confirm": (MemoryStatus.CANDIDATE, MemoryStatus.ACTIVE),
        "reject": (MemoryStatus.CANDIDATE, MemoryStatus.REJECTED),
        "revoke": (MemoryStatus.ACTIVE, MemoryStatus.REVOKED),
    }
    expected_status, target_status = statuses[action]
    command = MemoryTransition(
        memory_id=memory_id.strip(), target_status=target_status, expected_version=version,
        reason=reason.strip(), metadata={"console_action": {
            "actor": user_id, "action": action, "reason": reason.strip(),
            "at": datetime.now(timezone.utc).isoformat(),
        }},
    )
    try:
        repository = PostgresMemoryRepository(initialize_schema=False)
        updated, evidence = repository.transition_scoped(command, owners, expected_status=expected_status)
    except (MemoryNotFound, ScopeDenied, VersionConflict, InvalidTransition):
        raise
    except Exception:
        raise MemoryStorageUnavailable("长期记忆存储暂不可用，操作未能确认。请刷新后重试。") from None
    now = datetime.now(timezone.utc)
    # Reuse the public projection, exposing no host audit metadata or credentials.
    row = asdict(updated)
    return {
        "item": _item(row, [_evidence(asdict(value)) for value in evidence], now, manageable=True),
        "index_sync": "queued",
        "message": {"confirm": "记忆已确认。", "reject": "候选记忆已拒绝。", "revoke": "记忆已撤销。"}[action]
            + "语义索引更新已排队。",
    }
