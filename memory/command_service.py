from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone
from typing import Callable
from uuid import uuid4

from memory.commands import MemoryContext, MemoryTransition, MemoryWriteProposal
from memory.conflict_service import ConflictAction, MemoryConflictService
from memory.dedup import normalize_memory_text
from memory.domain import (
    MemoryEvidence,
    MemoryItem,
    MemorySourceType,
    MemoryStatus,
)
from memory.repository import MemoryNotFound, ScopeDenied


class MemoryCommandService:
    def __init__(
        self,
        repository,
        *,
        conflict_service: MemoryConflictService | None = None,
        trace: Callable[..., None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.conflict_service = conflict_service or MemoryConflictService()
        self.trace = trace
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.trace_events: list[dict] = []

    def drain_trace_events(self) -> list[dict]:
        events = list(self.trace_events)
        self.trace_events.clear()
        return events

    def remember(
        self,
        proposal: MemoryWriteProposal,
        context: MemoryContext,
    ) -> MemoryItem:
        self._authorize(proposal, context)
        if not proposal.explicit_user_request and proposal.source_type is not MemorySourceType.EXPLICIT_USER:
            raise ValueError("remember requires an explicit user request.")
        return self._create_or_merge(proposal, context, MemoryStatus.ACTIVE)

    def propose(
        self,
        proposal: MemoryWriteProposal,
        context: MemoryContext,
    ) -> MemoryItem:
        self._authorize(proposal, context)
        return self._create_or_merge(proposal, context, MemoryStatus.CANDIDATE)

    def record_verified_conclusion(
        self,
        proposal: MemoryWriteProposal,
        context: MemoryContext,
    ) -> MemoryItem:
        self._authorize(proposal, context)
        if proposal.source_type is not MemorySourceType.CODING_CONCLUSION:
            raise ValueError("verified conclusion must use CODING_CONCLUSION source.")
        if not proposal.evidence or not all(
            bool(evidence.metadata.get("verified")) for evidence in proposal.evidence
        ):
            raise ValueError("verified conclusion requires verified evidence.")
        return self._create_or_merge(proposal, context, MemoryStatus.ACTIVE)

    def confirm(self, memory_id: str, context: MemoryContext) -> MemoryItem:
        item = self._owned(memory_id, context)
        updated = self.repository.transition(MemoryTransition(
            memory_id=item.id,
            target_status=MemoryStatus.ACTIVE,
            expected_version=item.version,
            reason="user_confirmed",
        ))
        self._emit("memory.item.confirmed", updated, reason="user_confirmed")
        return updated

    def reject(self, memory_id: str, reason: str, context: MemoryContext) -> MemoryItem:
        item = self._owned(memory_id, context)
        updated = self.repository.transition(MemoryTransition(
            memory_id=item.id,
            target_status=MemoryStatus.REJECTED,
            expected_version=item.version,
            reason=reason,
        ))
        self._emit("memory.item.rejected", updated, reason=reason)
        return updated

    def update(
        self,
        memory_id: str,
        content: str,
        context: MemoryContext,
        *,
        evidence: tuple[MemoryEvidence, ...] = (),
    ) -> MemoryItem:
        old = self._owned(memory_id, context)
        if old.status is not MemoryStatus.ACTIVE:
            raise ValueError("Only active memory can be updated.")
        normalized = normalize_memory_text(content)
        if not normalized:
            raise ValueError("Updated memory content is empty after normalization.")
        now = self.clock()
        new_id = str(uuid4())
        new_item = MemoryItem(
            id=new_id,
            owner_scope=old.owner_scope,
            owner_id=old.owner_id,
            kind=old.kind,
            content=content,
            normalized_content=normalized,
            status=MemoryStatus.ACTIVE,
            confidence=old.confidence,
            salience=old.salience,
            valid_from=now,
            valid_until=old.valid_until,
            last_confirmed_at=now,
            supersedes_id=old.id,
            version=old.version + 1,
            created_at=now,
            updated_at=now,
            metadata={**old.metadata, "update_source": context.session_id},
        )
        bound_evidence = self._bind_evidence(evidence, new_id)
        updated = self.repository.supersede(
            old.id,
            new_item,
            bound_evidence,
            expected_version=old.version,
        )
        self._emit(
            "memory.item.superseded",
            updated,
            reason="updated",
            previous_memory_id=old.id,
        )
        self._emit(
            "memory.item.updated",
            updated,
            reason="explicit_update",
            previous_memory_id=old.id,
        )
        return updated

    def revoke(self, memory_id: str, reason: str, context: MemoryContext) -> MemoryItem:
        item = self._owned(memory_id, context)
        updated = self.repository.transition(MemoryTransition(
            memory_id=item.id,
            target_status=MemoryStatus.REVOKED,
            expected_version=item.version,
            reason=reason,
        ))
        self._emit("memory.item.revoked", updated, reason=reason)
        return updated

    def forget(self, query: str, context: MemoryContext) -> list[MemoryItem]:
        normalized = normalize_memory_text(query)
        if not normalized:
            return []
        active = self.repository.list_active(context.allowed_owners(), self.clock())
        matches = [
            item for item in active
            if normalized in item.normalized_content
            or item.normalized_content in normalized
        ]
        revoked = []
        for item in matches:
            revoked.append(self.revoke(item.id, "forget_request", context))
        return revoked

    def _create_or_merge(
        self,
        proposal: MemoryWriteProposal,
        context: MemoryContext,
        status: MemoryStatus,
    ) -> MemoryItem:
        now = self.clock()
        normalized = normalize_memory_text(proposal.content)
        if not normalized:
            raise ValueError("Memory content is empty after normalization.")
        exact = self.repository.find_exact(proposal.owner, proposal.kind, normalized)
        existing = [exact] if exact else self.repository.list_active([proposal.owner], now)
        decision = self.conflict_service.decide(proposal, existing)
        if decision.action in {ConflictAction.MERGE_EXACT, ConflictAction.MERGE_SEMANTIC}:
            assert decision.existing is not None
            item = self.repository.add_evidence(
                decision.existing.id,
                self._bind_evidence(proposal.evidence, decision.existing.id),
                expected_version=decision.existing.version,
            )
            self._emit(
                "memory.item.duplicate",
                item,
                reason=decision.reason,
            )
            return item
        if decision.action is ConflictAction.KEEP_CANDIDATE:
            status = MemoryStatus.CANDIDATE
        item_id = str(uuid4())
        item = MemoryItem(
            id=item_id,
            owner_scope=proposal.owner_scope,
            owner_id=proposal.owner_id,
            kind=proposal.kind,
            content=proposal.content,
            normalized_content=normalized,
            status=status,
            confidence=proposal.confidence,
            salience=proposal.salience,
            valid_from=now,
            valid_until=proposal.valid_until,
            last_confirmed_at=now if status is MemoryStatus.ACTIVE else None,
            supersedes_id=(decision.existing.id if decision.action is ConflictAction.SUPERSEDE and decision.existing else None),
            version=(decision.existing.version + 1 if decision.action is ConflictAction.SUPERSEDE and decision.existing else 1),
            created_at=now,
            updated_at=now,
            metadata={**proposal.metadata, "source_session_id": context.session_id},
        )
        evidence = self._bind_evidence(proposal.evidence, item.id)
        if decision.action is ConflictAction.SUPERSEDE and decision.existing is not None:
            stored = self.repository.supersede(
                decision.existing.id,
                item,
                evidence,
                expected_version=decision.existing.version,
            )
            self._emit(
                "memory.item.superseded",
                stored,
                reason=decision.reason,
                previous_memory_id=decision.existing.id,
            )
            return stored
        stored = self.repository.create(item, evidence)
        event = "memory.item.created" if status is MemoryStatus.ACTIVE else "memory.candidate.created"
        self._emit(event, stored, reason=decision.reason)
        return stored

    def _owned(self, memory_id: str, context: MemoryContext) -> MemoryItem:
        item = self.repository.get(memory_id)
        if item is None:
            raise MemoryNotFound(memory_id)
        if not context.permits(item.owner):
            raise ScopeDenied(f"Memory scope is not available in this context: {item.owner}")
        return item

    def _authorize(self, proposal: MemoryWriteProposal, context: MemoryContext) -> None:
        if not context.permits(proposal.owner):
            raise ScopeDenied(
                f"Proposal owner is not available in this context: {proposal.owner}"
            )

    def _bind_evidence(
        self,
        evidence: tuple[MemoryEvidence, ...] | list[MemoryEvidence],
        memory_id: str,
    ) -> tuple[MemoryEvidence, ...]:
        return tuple(replace(value, memory_id=memory_id) for value in evidence)

    def _emit(
        self,
        event: str,
        item: MemoryItem,
        *,
        reason: str,
        previous_memory_id: str | None = None,
    ) -> None:
        payload = {
            "memory_id": item.id,
            "owner_scope": item.owner_scope.value,
            "owner_id": item.owner_id,
            "kind": item.kind.value,
            "status": item.status.value,
            "version": item.version,
            "confidence": item.confidence,
            "salience": item.salience,
            "reason": reason,
            "previous_memory_id": previous_memory_id,
            "content_digest": hashlib.sha256(item.content.encode()).hexdigest()[:16],
            "content_preview": item.content[:160],
        }
        self.trace_events.append({"event": event, "payload": payload})
        if self.trace is None:
            return
        try:
            self.trace(event, payload)
        except TypeError:
            self.trace({"event": event, "payload": payload})
