"""Transactional context snapshots independent from TaskState patches."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
from typing import Any, Callable, Iterable, Sequence


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class SnapshotStatus(StrEnum):
    REQUESTED = "requested"
    GENERATING = "generating"
    PREPARED = "prepared"
    ACTIVE = "active"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class ContextSnapshot:
    snapshot_id: str
    session_id: str
    status: SnapshotStatus
    summary: str
    covered_event_start_seq: int
    covered_event_end_seq: int
    covered_event_start_id: str
    covered_event_end_id: str
    source_task_state_version: int
    generation: int
    strategy: str
    source_hash: str
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    attempt_log: tuple[dict[str, Any], ...] = ()
    archive_completed: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["artifact_refs"] = list(self.artifact_refs)
        payload["validation_errors"] = list(self.validation_errors)
        payload["attempt_log"] = list(self.attempt_log)
        return payload

    @classmethod
    def from_payload(cls, payload: Any) -> "ContextSnapshot | None":
        if not isinstance(payload, dict):
            return None
        try:
            return cls(
                snapshot_id=str(payload["snapshot_id"]),
                session_id=str(payload["session_id"]),
                status=SnapshotStatus(str(payload["status"])),
                summary=str(payload.get("summary") or ""),
                covered_event_start_seq=int(payload.get("covered_event_start_seq") or 0),
                covered_event_end_seq=int(payload.get("covered_event_end_seq") or 0),
                covered_event_start_id=str(payload.get("covered_event_start_id") or ""),
                covered_event_end_id=str(payload.get("covered_event_end_id") or ""),
                source_task_state_version=max(1, int(payload.get("source_task_state_version") or 1)),
                generation=max(1, int(payload.get("generation") or 1)),
                strategy=str(payload.get("strategy") or "deterministic"),
                source_hash=str(payload.get("source_hash") or ""),
                evidence_refs=tuple(str(item) for item in payload.get("evidence_refs") or []),
                artifact_refs=tuple(str(item) for item in payload.get("artifact_refs") or []),
                validation_errors=tuple(str(item) for item in payload.get("validation_errors") or []),
                attempt_log=tuple(dict(item) for item in payload.get("attempt_log") or []),
                archive_completed=bool(payload.get("archive_completed")),
                created_at=str(payload.get("created_at") or _now()),
                updated_at=str(payload.get("updated_at") or _now()),
            )
        except (KeyError, TypeError, ValueError):
            return None


@dataclass(frozen=True)
class CompactionLimits:
    normal_attempts: int = 1
    repair_attempts: int = 1
    chunked_attempts: int = 1
    deterministic_fallback: bool = True
    summary_max_chars: int = 12_000
    chunk_max_chars: int = 24_000


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    strategy: str
    attempt_log: tuple[dict[str, Any], ...]
    validation_errors: tuple[str, ...] = ()


class SnapshotValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(str(item) for item in errors)
        super().__init__("; ".join(self.errors))


class SnapshotValidator:
    def validate(
        self,
        summary: str,
        *,
        objective: str,
        constraints: Sequence[str],
        pending: Sequence[str],
        evidence_refs: Sequence[str],
        artifact_refs: Sequence[str],
        max_chars: int,
        covered_start_seq: int,
        covered_end_seq: int,
        source_task_state_version: int,
    ) -> None:
        text = str(summary or "").strip()
        errors: list[str] = []
        if not text:
            errors.append("summary is empty")
        if len(text) > max(1, int(max_chars)):
            errors.append("summary exceeds budget")
        if covered_start_seq <= 0 or covered_end_seq < covered_start_seq:
            errors.append("covered event range is invalid")
        if source_task_state_version < 1:
            errors.append("source TaskState version is invalid")
        if objective and not _contains_anchor(text, objective):
            errors.append("global objective is missing")
        for label, values in (
            ("constraint", constraints),
            ("pending item", pending),
            ("evidence reference", evidence_refs),
            ("artifact reference", artifact_refs),
        ):
            for value in values:
                if value and not _contains_anchor(text, value):
                    errors.append(f"{label} is missing: {value[:80]}")
        if errors:
            raise SnapshotValidationError(errors)


class EventCompactor:
    """Bounded summary generator. It never proposes or applies TaskState patches."""

    def __init__(
        self,
        *,
        provider=None,
        model: str = "",
        limits: CompactionLimits | None = None,
        validator: SnapshotValidator | None = None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.limits = limits or CompactionLimits()
        self.validator = validator or SnapshotValidator()

    def compact(
        self,
        *,
        events: Sequence[dict[str, Any]],
        task_state: Any,
        covered_start_seq: int,
        covered_end_seq: int,
    ) -> CompactionOutput:
        facts = _task_facts(task_state)
        refs = _event_refs(events)
        validation = {
            "objective": facts["objective"],
            "constraints": facts["constraints"],
            "pending": facts["pending"],
            "evidence_refs": refs["evidence_refs"],
            "artifact_refs": refs["artifact_refs"],
            "max_chars": self.limits.summary_max_chars,
            "covered_start_seq": covered_start_seq,
            "covered_end_seq": covered_end_seq,
            "source_task_state_version": int(getattr(task_state, "version", 1) or 1),
        }
        attempts: list[dict[str, Any]] = []
        last_errors: tuple[str, ...] = ()

        if self.provider is not None:
            for _ in range(max(0, self.limits.normal_attempts)):
                try:
                    summary = self._invoke(_normal_prompt(events, facts, refs))
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "normal", "status": "success"})
                    return CompactionOutput(summary, "normal", tuple(attempts))
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({
                        "strategy": "normal",
                        "status": "failed",
                        "errors": list(last_errors),
                    })

            for _ in range(max(0, self.limits.repair_attempts)):
                try:
                    summary = self._invoke(_repair_prompt(events, facts, refs, last_errors))
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "repair", "status": "success"})
                    return CompactionOutput(summary, "repair", tuple(attempts))
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({
                        "strategy": "repair",
                        "status": "failed",
                        "errors": list(last_errors),
                    })

            for _ in range(max(0, self.limits.chunked_attempts)):
                try:
                    summaries = [
                        self._invoke(_normal_prompt(chunk, facts, _event_refs(chunk)))
                        for chunk in _chunk_events(events, self.limits.chunk_max_chars)
                    ]
                    summary = _combine_chunk_summaries(summaries, facts, refs)
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "chunked", "status": "success"})
                    return CompactionOutput(summary, "chunked", tuple(attempts))
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({
                        "strategy": "chunked",
                        "status": "failed",
                        "errors": list(last_errors),
                    })

        if not self.limits.deterministic_fallback:
            raise SnapshotValidationError(last_errors or ("compaction attempts exhausted",))
        summary = _deterministic_summary(events, facts, refs, last_errors)
        self.validator.validate(summary, **validation)
        attempts.append({"strategy": "deterministic", "status": "success"})
        return CompactionOutput(
            summary,
            "deterministic",
            tuple(attempts),
            last_errors,
        )

    def _invoke(self, prompt: str) -> str:
        response = self.provider.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            tool_choice="none",
            max_tokens=max(256, self.limits.summary_max_chars // 4),
        )
        return str(getattr(response, "content", "") or "").strip()


class ContextSnapshotManager:
    """Two-phase snapshot activation with deterministic crash recovery."""

    def __init__(
        self,
        *,
        prepare_writer: Callable[[Any, ContextSnapshot], Any] | None = None,
        activation_writer: Callable[[Any, ContextSnapshot], Any] | None = None,
        archive_writer: Callable[[Any, ContextSnapshot], Any] | None = None,
    ) -> None:
        self.prepare_writer = prepare_writer
        self.activation_writer = activation_writer
        self.archive_writer = archive_writer

    def prepare(
        self,
        session,
        *,
        output: CompactionOutput,
        events: Sequence[Any],
        source_task_state_version: int,
        evidence_refs: Sequence[str],
        artifact_refs: Sequence[str],
    ) -> ContextSnapshot:
        if not events:
            raise ValueError("a snapshot must cover at least one event")
        start, end = events[0], events[-1]
        source_hash = _source_hash(events, source_task_state_version)
        snapshot_id = "snapshot_" + hashlib.sha256(
            f"{session.id}:{source_hash}".encode("utf-8")
        ).hexdigest()[:32]
        existing = _snapshot_by_id(session, snapshot_id)
        if existing is not None:
            return existing
        generation = 1 + max(
            (item.generation for item in _session_snapshots(session)),
            default=0,
        )
        snapshot = ContextSnapshot(
            snapshot_id=snapshot_id,
            session_id=str(session.id),
            status=SnapshotStatus.PREPARED,
            summary=output.summary,
            covered_event_start_seq=int(_field(start, "seq", 0) or 0),
            covered_event_end_seq=int(_field(end, "seq", 0) or 0),
            covered_event_start_id=str(_field(start, "event_id", "") or ""),
            covered_event_end_id=str(_field(end, "event_id", "") or ""),
            source_task_state_version=max(1, int(source_task_state_version)),
            generation=generation,
            strategy=output.strategy,
            source_hash=source_hash,
            evidence_refs=tuple(str(item) for item in evidence_refs if item),
            artifact_refs=tuple(str(item) for item in artifact_refs if item),
            validation_errors=output.validation_errors,
            attempt_log=output.attempt_log,
        )
        previous = list(getattr(session, "context_snapshots", []) or [])
        _replace_snapshot(session, snapshot)
        try:
            if self.prepare_writer is not None:
                self.prepare_writer(session, snapshot)
        except Exception:
            session.context_snapshots = previous
            raise
        return snapshot

    def activate(self, session, snapshot_id: str) -> ContextSnapshot:
        snapshot = _snapshot_by_id(session, snapshot_id)
        if snapshot is None:
            raise KeyError(f"unknown ContextSnapshot: {snapshot_id}")
        if snapshot.status is SnapshotStatus.ACTIVE:
            return self._archive(session, snapshot)
        if snapshot.status is not SnapshotStatus.PREPARED:
            raise ValueError(f"snapshot cannot be activated from {snapshot.status.value}")
        previous = list(getattr(session, "context_snapshots", []) or [])
        previous_active_id = str(getattr(session, "active_snapshot_id", "") or "")
        for item in _session_snapshots(session):
            if item.status is SnapshotStatus.ACTIVE:
                _replace_snapshot(session, _with_status(item, SnapshotStatus.SUPERSEDED))
        snapshot = _with_status(snapshot, SnapshotStatus.ACTIVE)
        _replace_snapshot(session, snapshot)
        session.active_snapshot_id = snapshot.snapshot_id
        try:
            if self.activation_writer is not None:
                self.activation_writer(session, snapshot)
        except Exception:
            session.context_snapshots = previous
            session.active_snapshot_id = previous_active_id
            raise
        return self._archive(session, snapshot)

    def recover(self, session) -> ContextSnapshot | None:
        active = _snapshot_by_id(session, str(getattr(session, "active_snapshot_id", "") or ""))
        if active is not None and not active.archive_completed:
            return self._archive(session, active)
        prepared = sorted(
            (item for item in _session_snapshots(session) if item.status is SnapshotStatus.PREPARED),
            key=lambda item: (item.generation, item.created_at),
        )
        if prepared:
            return self.activate(session, prepared[-1].snapshot_id)
        return active

    def _archive(self, session, snapshot: ContextSnapshot) -> ContextSnapshot:
        if snapshot.archive_completed:
            return snapshot
        session.set_archive_boundary(snapshot.covered_event_end_seq)
        completed = ContextSnapshot(**{
            **snapshot.to_dict(),
            "status": snapshot.status,
            "evidence_refs": snapshot.evidence_refs,
            "artifact_refs": snapshot.artifact_refs,
            "validation_errors": snapshot.validation_errors,
            "attempt_log": snapshot.attempt_log,
            "archive_completed": True,
            "updated_at": _now(),
        })
        if self.archive_writer is not None:
            self.archive_writer(session, completed)
        _replace_snapshot(session, completed)
        return completed


def active_context_snapshot(session) -> ContextSnapshot | None:
    return _snapshot_by_id(session, str(getattr(session, "active_snapshot_id", "") or ""))


def _session_snapshots(session) -> list[ContextSnapshot]:
    snapshots = []
    for payload in getattr(session, "context_snapshots", []) or []:
        item = payload if isinstance(payload, ContextSnapshot) else ContextSnapshot.from_payload(payload)
        if item is not None:
            snapshots.append(item)
    return snapshots


def _snapshot_by_id(session, snapshot_id: str) -> ContextSnapshot | None:
    return next((item for item in _session_snapshots(session) if item.snapshot_id == snapshot_id), None)


def _replace_snapshot(session, snapshot: ContextSnapshot) -> None:
    items = [
        item.to_dict()
        for item in _session_snapshots(session)
        if item.snapshot_id != snapshot.snapshot_id
    ]
    items.append(snapshot.to_dict())
    items.sort(key=lambda item: (int(item.get("generation") or 0), str(item.get("created_at") or "")))
    session.context_snapshots = items
    session.touch()


def _with_status(snapshot: ContextSnapshot, status: SnapshotStatus) -> ContextSnapshot:
    payload = snapshot.to_dict()
    payload.update(status=status.value, updated_at=_now())
    restored = ContextSnapshot.from_payload(payload)
    assert restored is not None
    return restored


def _task_facts(state: Any) -> dict[str, list[str] | str]:
    objective = getattr(state, "objective", "")
    objective = getattr(objective, "summary", objective)
    return {
        "objective": str(objective or ""),
        "constraints": [_item_text(item) for item in getattr(state, "constraints", []) or []],
        "pending": [_item_text(item) for item in getattr(state, "pending_actions", []) or []],
        "findings": [_item_text(item) for item in getattr(state, "findings", []) or []],
        "decisions": [_item_text(item) for item in getattr(state, "decisions", []) or []],
        "blockers": [_item_text(item) for item in getattr(state, "blockers", []) or []],
    }


def _item_text(item: Any) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, dict):
        for key in ("description", "claim", "summary", "text", "reason", "value"):
            if item.get(key):
                return str(item[key])
        return json.dumps(item, ensure_ascii=False, default=str)
    for key in ("description", "claim", "summary", "text", "reason", "value"):
        value = getattr(item, key, None)
        if value:
            return str(value)
    return str(item)


def _event_refs(events: Sequence[dict[str, Any]]) -> dict[str, list[str]]:
    evidence: list[str] = []
    artifacts: list[str] = []
    for event in events:
        text = json.dumps(event, ensure_ascii=False, default=str)
        for prefix, target in (("evidence://", evidence), ("artifact://", artifacts)):
            for token in text.replace('"', " ").replace("'", " ").split():
                if token.startswith(prefix):
                    target.append(token.rstrip(",;)]}"))
    return {
        "evidence_refs": list(dict.fromkeys(evidence)),
        "artifact_refs": list(dict.fromkeys(artifacts)),
    }


def _normal_prompt(events, facts, refs) -> str:
    return (
        "Compress these completed context events. Preserve the exact global objective, "
        "constraints, pending work, findings, decisions, blockers, evidence/artifact refs, "
        "important tool results, file locations, and recent interaction continuity. "
        "Return summary text only; do not propose state changes.\n"
        f"TASK FACTS:\n{json.dumps(facts, ensure_ascii=False, default=str)}\n"
        f"REFERENCES:\n{json.dumps(refs, ensure_ascii=False)}\n"
        f"EVENTS:\n{json.dumps(events, ensure_ascii=False, default=str)}"
    )


def _repair_prompt(events, facts, refs, errors) -> str:
    return (
        _normal_prompt(events, facts, refs)
        + "\nThe prior summary was rejected. Correct every validation error: "
        + json.dumps(list(errors), ensure_ascii=False)
    )


def _deterministic_summary(events, facts, refs, errors) -> str:
    lines = [
        "[Deterministic context snapshot: model compaction unavailable or invalid]",
        f"Global objective: {facts['objective']}",
    ]
    for title, key in (
        ("Constraints", "constraints"),
        ("Pending work", "pending"),
        ("Findings", "findings"),
        ("Decisions", "decisions"),
        ("Blockers", "blockers"),
        ("Evidence references", "evidence_refs"),
        ("Artifact references", "artifact_refs"),
    ):
        values = facts.get(key, refs.get(key, []))
        lines.append(f"{title}:")
        lines.extend(f"- {value}" for value in values) if values else lines.append("- (none)")
    lines.append("Recent covered events:")
    for event in list(events)[-12:]:
        lines.append("- " + _squash(json.dumps(event, ensure_ascii=False, default=str), 900))
    if errors:
        lines.append("Compaction failures: " + "; ".join(errors))
    return "\n".join(lines)


def _combine_chunk_summaries(summaries, facts, refs) -> str:
    return _deterministic_summary(
        [{"chunk_summary": summary} for summary in summaries],
        facts,
        refs,
        (),
    )


def _chunk_events(events: Sequence[dict[str, Any]], max_chars: int) -> list[list[dict[str, Any]]]:
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for event in events:
        event_size = len(json.dumps(event, ensure_ascii=False, default=str))
        if current and size + event_size > max(1, int(max_chars)):
            chunks.append(current)
            current, size = [], 0
        current.append(event)
        size += event_size
    if current:
        chunks.append(current)
    return chunks


def _source_hash(events: Sequence[Any], task_state_version: int) -> str:
    payload = [
        {
            "seq": _field(event, "seq", 0),
            "event_id": _field(event, "event_id", ""),
            "payload": _field(event, "payload", {}),
        }
        for event in events
    ]
    return hashlib.sha256(json.dumps(
        {"events": payload, "task_state_version": task_state_version},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")).hexdigest()


def _validation_errors(exc: Exception) -> tuple[str, ...]:
    if isinstance(exc, SnapshotValidationError):
        return exc.errors
    return (f"{type(exc).__name__}: {exc}",)


def _contains_anchor(text: str, value: str) -> bool:
    anchor = " ".join(str(value or "").lower().split())[:80]
    haystack = " ".join(str(text or "").lower().split())
    return not anchor or anchor in haystack


def _squash(text: str, limit: int) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= limit else value[: max(1, limit - 3)] + "..."


def _field(item: Any, name: str, default=None):
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


__all__ = (
    "CompactionLimits",
    "CompactionOutput",
    "ContextSnapshot",
    "ContextSnapshotManager",
    "EventCompactor",
    "SnapshotStatus",
    "SnapshotValidationError",
    "SnapshotValidator",
    "active_context_snapshot",
)
