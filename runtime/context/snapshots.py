"""Transactional context snapshots independent from TaskState patches."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
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
    retained_observations: tuple[str, ...] = ()
    previous_snapshot_id: str = ""
    lineage_version: int = 0
    validation_errors: tuple[str, ...] = ()
    attempt_log: tuple[dict[str, Any], ...] = ()
    archive_completed: bool = False
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    working_memory: tuple[str, ...] = ()
    model_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["status"] = self.status.value
        payload["evidence_refs"] = list(self.evidence_refs)
        payload["artifact_refs"] = list(self.artifact_refs)
        payload["retained_observations"] = list(self.retained_observations)
        payload["validation_errors"] = list(self.validation_errors)
        payload["attempt_log"] = list(self.attempt_log)
        payload["working_memory"] = list(self.working_memory)
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
                retained_observations=tuple(str(item) for item in payload.get("retained_observations") or []),
                previous_snapshot_id=str(payload.get("previous_snapshot_id") or ""),
                lineage_version=int(payload.get("lineage_version") or 0),
                validation_errors=tuple(str(item) for item in payload.get("validation_errors") or []),
                attempt_log=tuple(dict(item) for item in payload.get("attempt_log") or []),
                archive_completed=bool(payload.get("archive_completed")),
                created_at=str(payload.get("created_at") or _now()),
                updated_at=str(payload.get("updated_at") or _now()),
                working_memory=tuple(str(item) for item in payload.get("working_memory") or []),
                model_notes=str(payload.get("model_notes") or ""),
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
    observations_max_chars: int = 4_000
    working_memory_max_chars: int = 3_000
    references_max_chars: int = 1_600
    prompt_max_chars: int = 48_000
    max_chunks: int = 8


@dataclass(frozen=True)
class CompactionOutput:
    summary: str
    strategy: str
    attempt_log: tuple[dict[str, Any], ...]
    validation_errors: tuple[str, ...] = ()
    retained_observations: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    previous_snapshot_id: str = ""
    lineage_version: int = 1
    working_memory: tuple[str, ...] = ()
    model_notes: str = ""


class SnapshotValidationError(ValueError):
    def __init__(self, errors: Sequence[str]) -> None:
        self.errors = tuple(str(item) for item in errors)
        super().__init__("; ".join(self.errors))


class SnapshotValidator:
    """Structural completeness only; anchor presence is not semantic verification."""
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
        previous_snapshot: ContextSnapshot | None = None,
    ) -> CompactionOutput:
        facts = _task_facts(task_state)
        refs = _event_refs(events)
        if previous_snapshot is not None:
            for key in ("evidence_refs", "artifact_refs"):
                refs[key] = list(dict.fromkeys([
                    *getattr(previous_snapshot, key), *refs[key],
                ]))
        # The complete reference index stays on disk. Only its bounded hot
        # projection belongs in a prompt; old sources remain addressable.
        recovery = _recovery_hint(previous_snapshot, covered_end_seq)
        empty_refs = {"evidence_refs": [], "artifact_refs": []}
        baseline = _render_summary(facts, empty_refs, (), (), recovery)
        if len(baseline) > self.limits.summary_max_chars:
            raise SnapshotValidationError(("protected context exceeds budget; raw events retained",))
        available = self.limits.summary_max_chars - len(baseline)
        reference_budget = min(max(0, self.limits.references_max_chars), available // 4)
        visible_refs = _bounded_refs(refs, reference_budget)
        reference_chars = len(_render_summary(facts, visible_refs, (), (), recovery)) - len(baseline)
        memory_budget = min(max(0, self.limits.working_memory_max_chars), (available - reference_chars) // 2)
        observation_budget = min(
            max(0, self.limits.observations_max_chars),
            available - reference_chars - memory_budget,
        )
        candidates = _retained_observations(events, previous_snapshot)
        observations = _bounded_records(candidates, observation_budget)
        prior_memory = _working_memory(events, previous_snapshot)
        memory = _bounded_records(prior_memory, memory_budget, prioritize=True)
        prompt_facts = {
            **facts, "prior_observations": list(observations),
            "unverified_working_memory": list(memory), "archive_recovery": recovery,
        }
        validation = {
            "objective": facts["objective"],
            "constraints": facts["constraints"],
            "pending": facts["pending"],
            "evidence_refs": visible_refs["evidence_refs"],
            "artifact_refs": visible_refs["artifact_refs"],
            "max_chars": self.limits.summary_max_chars,
            "covered_start_seq": covered_start_seq,
            "covered_end_seq": covered_end_seq,
            "source_task_state_version": int(getattr(task_state, "version", 1) or 1),
        }

        def output(notes: str, strategy: str, attempts, errors=()) -> CompactionOutput:
            # Derived reasoning is never promoted to TaskState, even if it says
            # "verified". Both hypotheses and their counterevidence are carried.
            final_memory = _bounded_records(
                [*prior_memory, *_model_memory(notes, facts, visible_refs, events)],
                memory_budget, prioritize=True,
            )
            summary = _render_summary(facts, visible_refs, observations, final_memory, recovery)
            self.validator.validate(summary, **validation)
            return CompactionOutput(
                summary=summary, strategy=strategy, attempt_log=tuple(attempts),
                validation_errors=tuple(errors), retained_observations=observations,
                evidence_refs=tuple(refs["evidence_refs"]), artifact_refs=tuple(refs["artifact_refs"]),
                previous_snapshot_id=previous_snapshot.snapshot_id if previous_snapshot else "",
                working_memory=final_memory, model_notes=notes,
            )

        attempts: list[dict[str, Any]] = []
        last_errors: tuple[str, ...] = ()

        if self.provider is not None:
            for _ in range(max(0, self.limits.normal_attempts)):
                try:
                    summary = self._invoke(_normal_prompt(events, prompt_facts, visible_refs))
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "normal", "status": "success"})
                    return output(summary, "normal", attempts)
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({"strategy": "normal", "status": "failed", "errors": list(last_errors)})

            for _ in range(max(0, self.limits.repair_attempts)):
                try:
                    summary = self._invoke(_repair_prompt(events, prompt_facts, visible_refs, last_errors))
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "repair", "status": "success"})
                    return output(summary, "repair", attempts)
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({"strategy": "repair", "status": "failed", "errors": list(last_errors)})

            for _ in range(max(0, self.limits.chunked_attempts)):
                try:
                    overhead = len(_normal_prompt([], prompt_facts, visible_refs))
                    chunk_budget = min(self.limits.chunk_max_chars, self.limits.prompt_max_chars - overhead)
                    if chunk_budget < 256:
                        raise SnapshotValidationError(("protected compaction prompt exceeds budget",))
                    chunks = _chunk_events(events, chunk_budget)
                    if len(chunks) > max(0, self.limits.max_chunks):
                        raise SnapshotValidationError(("compaction chunk call budget exhausted",))
                    summaries = [
                        self._invoke(_normal_prompt(chunk, prompt_facts, visible_refs)) for chunk in chunks
                    ]
                    summary = _combine_chunk_summaries(summaries, facts, visible_refs)
                    self.validator.validate(summary, **validation)
                    attempts.append({"strategy": "chunked", "status": "success"})
                    return output("\n".join(summaries), "chunked", attempts)
                except Exception as exc:
                    last_errors = _validation_errors(exc)
                    attempts.append({"strategy": "chunked", "status": "failed", "errors": list(last_errors)})

        if not self.limits.deterministic_fallback:
            raise SnapshotValidationError(last_errors or ("compaction attempts exhausted",))
        attempts.append({"strategy": "deterministic", "status": "success"})
        return output("", "deterministic", attempts, last_errors)

    def _invoke(self, prompt: str) -> str:
        if len(prompt) > max(1, self.limits.prompt_max_chars):
            raise SnapshotValidationError(("compaction prompt exceeds budget",))
        response = self.provider.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            tool_choice="none",
            max_tokens=max(256, self.limits.summary_max_chars // 4),
        )
        text = str(getattr(response, "content", "") or "").strip()
        if len(text) > self.limits.summary_max_chars:
            raise SnapshotValidationError(("model summary exceeds budget",))
        return text


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
        if output.lineage_version and output.previous_snapshot_id != str(
            getattr(session, "active_snapshot_id", "") or ""
        ):
            raise SnapshotValidationError(("snapshot parent changed during compaction",))
        start, end = events[0], events[-1]
        source_hash = _source_hash(events, source_task_state_version)
        if output.previous_snapshot_id:
            source_hash = hashlib.sha256(
                f"{source_hash}:{output.previous_snapshot_id}".encode("utf-8")
            ).hexdigest()
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
            evidence_refs=tuple(dict.fromkeys([
                *(str(item) for item in evidence_refs if item), *output.evidence_refs,
            ])),
            artifact_refs=tuple(dict.fromkeys([
                *(str(item) for item in artifact_refs if item), *output.artifact_refs,
            ])),
            retained_observations=output.retained_observations,
            working_memory=output.working_memory,
            model_notes=output.model_notes,
            previous_snapshot_id=output.previous_snapshot_id,
            lineage_version=output.lineage_version,
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
        if snapshot.lineage_version and snapshot.previous_snapshot_id != str(
            getattr(session, "active_snapshot_id", "") or ""
        ):
            raise SnapshotValidationError(("snapshot parent changed before activation",))
        if snapshot.covered_event_end_seq < int(getattr(session, "archive_boundary_seq", 0) or 0):
            raise SnapshotValidationError(("snapshot would move archive boundary backwards",))
        task_payload = (getattr(session, "metadata", {}) or {}).get("task_state", {})
        core = task_payload.get("core", task_payload) if isinstance(task_payload, dict) else {}
        current_version = core.get("version") if isinstance(core, dict) else None
        if current_version is not None and int(current_version) != snapshot.source_task_state_version:
            raise SnapshotValidationError(("TaskState changed before snapshot activation",))
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
            "retained_observations": snapshot.retained_observations,
            "working_memory": snapshot.working_memory,
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


def _event_text(event: dict[str, Any]) -> str:
    payload = event.get("payload", {})
    message = payload.get("message", payload) if isinstance(payload, dict) else payload
    content = message.get("content", message) if isinstance(message, dict) else message
    return content if isinstance(content, str) else json.dumps(content, ensure_ascii=False, default=str)


def _record(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _retained_observations(events, previous: ContextSnapshot | None) -> tuple[str, ...]:
    observations = list(previous.retained_observations) if previous else []
    for event in events:
        event_type = str(event.get("event_type") or event.get("type") or "")
        if event_type in {
            "task_state_checkpoint", "run_checkpoint", "compaction_completed",
            "assistant_message", "user_message", "user_correction", "tool_call",
        }:
            continue
        text = _event_text(event)
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise SnapshotValidationError(("retained observation requires a source event id",))
        observations.append(_record({
            "source_ref": f"event://{event_id}", "event_type": event_type,
            "observation": _squash(text, 600),
            "content_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "truncated": len(" ".join(text.split())) > 600,
        }))
    return tuple(observations)


_REASONING_PATTERNS = (
    ("counterevidence", r"counter[- ]?evidence|ruled? out|disprov|refut|contradict|反证|排除|证伪|推翻"),
    ("root_cause", r"root[- ]cause|根因|根本原因"),
    ("hypothesis", r"hypothes[ie]s|suspect|假设|猜测|怀疑"),
    ("next_step", r"next step|next experiment|下一步|下一项实验"),
)


def _reasoning_kind(text: str) -> tuple[str, int]:
    # These are retrieval/prioritization hints, never truth classification.
    for kind, pattern in _REASONING_PATTERNS:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            return kind, match.start()
    return "model_note", 0


def _memory_record(text: str, *, source_ref: str, origin: str) -> str:
    kind, offset = _reasoning_kind(text)
    # A long log prefix must not hide the debugging conclusion at its end.
    normalized = " ".join(text.split())
    start = max(0, offset - 160) if len(text) > 1_200 else 0
    excerpt = text[start:]
    provenance_key = "input_ref" if origin == "model_notes_derived_from_context" else "source_ref"
    return _record({
        "kind": kind, "status": "unverified", "origin": origin,
        provenance_key: source_ref, "text": _squash(excerpt, 1_200),
        "truncated": start > 0 or len(normalized) > 1_200,
    })


def _working_memory(events, previous: ContextSnapshot | None) -> tuple[str, ...]:
    memory = list(previous.working_memory) if previous else []
    if previous is not None and not memory:
        heading = "Unverified model notes (cannot change task facts):"
        if not previous.lineage_version and previous.summary:
            memory.append(_memory_record(
                f"Legacy unverified snapshot {previous.snapshot_id}: {previous.summary}",
                source_ref=f"snapshot://{previous.snapshot_id}", origin="legacy_snapshot",
            ))
        elif heading in previous.summary:
            notes = previous.summary.split(heading, 1)[1].split("COLD CONTEXT (", 1)[0].strip()
            if notes:
                memory.append(_memory_record(notes, source_ref=f"snapshot://{previous.snapshot_id}", origin="legacy_model_notes"))
    for event in events:
        if str(event.get("event_type") or event.get("type") or "") != "assistant_message":
            continue
        text = _event_text(event)
        kind, _ = _reasoning_kind(text)
        if kind == "model_note":
            continue
        event_id = str(event.get("event_id") or "")
        if event_id:
            memory.append(_memory_record(text, source_ref=f"event://{event_id}", origin="assistant_reasoning"))
    return tuple(memory)


def _model_memory(notes, facts, refs, events) -> tuple[str, ...]:
    if not notes:
        return ()
    # Repeating protected task fields is not useful working memory. Preserve
    # novel reasoning as derived notes, with input provenance, not as evidence.
    protected_lines = set(_protected_summary(facts, refs, ()).splitlines())
    source = next((str(item.get("event_id")) for item in reversed(events) if item.get("event_id")), "")
    result = []
    for line in str(notes).splitlines():
        line = _unwrap_memory_echo(line.strip())
        if line.startswith((
            "COLD CONTEXT (", "Original events: read_artifact(",
            "Read any event:// reference", "Earlier notes and complete reference indexes:",
            "Unverified model notes (cannot change task facts):",
        )):
            continue
        if not line or line in protected_lines or line.rstrip(":") in {
            "Constraints", "Pending work", "Findings", "Decisions", "Blockers",
            "Evidence references", "Artifact references", "Recent covered events",
        }:
            continue
        result.append(_memory_record(
            line, source_ref=f"event://{source}" if source else "",
            origin="model_notes_derived_from_context",
        ))
    return tuple(result)


def _unwrap_memory_echo(text: str) -> str:
    # Models often copy the JSON records in the prompt. Rewrapping those on
    # every generation grows escape sequences and displaces the original note.
    # Only the text is reused; copied authority and provenance are not trusted.
    for _ in range(4):
        try:
            payload = json.loads(text)
        except (TypeError, ValueError):
            break
        if isinstance(payload, str):
            text = payload
        elif isinstance(payload, dict) and "text" in payload and {"kind", "status", "origin"} <= payload.keys():
            text = str(payload["text"])
        elif isinstance(payload, dict) and {"observation", "event_type", "source_ref"} <= payload.keys():
            return ""
        else:
            break
    return text


def _bounded_records(records, max_chars: int, *, prioritize: bool = False) -> tuple[str, ...]:
    """A hot cache, not an archive: evicted entries remain in source snapshots."""
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in records:
        try:
            item = json.loads(raw)
        except (TypeError, ValueError):
            item = {"text": str(raw), "status": "unverified", "origin": "legacy"}
        if not isinstance(item, dict):
            continue
        key = (str(item.get("event_type") or item.get("kind") or ""), str(
            item.get("content_hash") or item.get("observation") or item.get("text") or ""
        ))
        old = unique.pop(key, None)
        if old is not None:
            if "observation" in item:
                item["repeat_count"] = int(old.get("repeat_count") or 1) + 1
            provenance_key = "source_ref" if old.get("source_ref") else "input_ref"
            latest_key = "latest_" + provenance_key
            latest_ref = item.get(latest_key) or item.get(provenance_key)
            if latest_ref and latest_ref != old.get(provenance_key):
                item[latest_key] = latest_ref
            if old.get(provenance_key):
                item[provenance_key] = old[provenance_key]
        unique[key] = item
    items = list(unique.values())
    priority = {"counterevidence": 4, "root_cause": 3, "hypothesis": 2, "next_step": 1}
    ordered = sorted(range(len(items)), key=lambda index: (
        priority.get(items[index].get("kind"), 0) if prioritize else 0, index,
    ), reverse=True)
    if prioritize and items:
        # Reserve room for a fresh line of investigation before reusing older
        # high-priority refutations; otherwise old counterevidence can starve it.
        newest = len(items) - 1
        ordered = [newest, *(index for index in ordered if index != newest)]
    selected: dict[int, str] = {}
    remaining = max(0, max_chars)
    for index in ordered:
        rendered = _record(items[index])
        if len(rendered) + 1 <= remaining:
            selected[index] = rendered
            remaining -= len(rendered) + 1
    return tuple(selected[index] for index in sorted(selected))


def _bounded_refs(refs, max_chars: int) -> dict[str, list[str]]:
    result = {"evidence_refs": [], "artifact_refs": []}
    remaining = max(0, max_chars)
    # Interleave indexes so a large evidence index cannot starve artifacts.
    candidates = []
    count = max((len(values) for values in refs.values()), default=0)
    for offset in range(1, count + 1):
        for key, values in refs.items():
            if len(values) >= offset:
                candidates.append((key, values[-offset]))
    for key, value in candidates:
        cost = len(value) + 3
        if cost <= remaining:
            result[key].insert(0, value)
            remaining -= cost
    return result


def _recovery_hint(previous: ContextSnapshot | None, covered_end_seq: int) -> str:
    lines = [
        "COLD CONTEXT (hot previews and notes are incomplete):",
        f"Original events: read_artifact(artifact_ref='event://session', start_seq=1, end_seq={covered_end_seq}, query='keywords').",
        "Read any event:// reference for original content; prior test results may need revalidation after edits.",
    ]
    if previous is not None:
        lines.append(f"Earlier notes and complete reference indexes: read_artifact(artifact_ref='snapshot://{previous.snapshot_id}'); follow previous_snapshot_id for older generations.")
    return "\n".join(lines)


def _render_summary(facts, refs, observations, memory, recovery) -> str:
    return (
        _protected_summary(facts, refs, observations)
        + "\nUnverified model notes (cannot change task facts):\n"
        + "\n".join(memory) + "\n" + recovery
    )


def _protected_summary(facts, refs, observations) -> str:
    lines = [
        "[Deterministic task projection; observations are not verified claims]",
        f"Global objective: {facts['objective']}",
    ]
    for title, key in (
        ("Constraints", "constraints"), ("Pending work", "pending"),
        ("Findings", "findings"), ("Decisions", "decisions"),
        ("Blockers", "blockers"), ("Evidence references", "evidence_refs"),
        ("Artifact references", "artifact_refs"),
    ):
        lines.append(title + ":")
        lines.extend("- " + value for value in facts.get(key, refs.get(key, [])))
    lines.append("RETAINED SOURCE OBSERVATIONS:")
    lines.extend(observations)
    return "\n".join(lines)


def _normal_prompt(events, facts, refs) -> str:
    return (
        "Compress these completed context events. Preserve the exact global objective, "
        "constraints, pending work, findings, decisions, blockers, evidence/artifact refs, "
        "important tool results, file locations, and recent interaction continuity. "
        "Working memory is unverified: preserve hypotheses, root causes, counterevidence "
        "and next experiments with their uncertainty and provenance. Do not turn earlier "
        "test outcomes into claims about edited code. Sources may be excerpts. "
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
    budget = max(1, int(max_chars))
    bounded_events = []
    for event in events:
        text = json.dumps(event, ensure_ascii=False, default=str)
        if len(text) + 2 <= budget:
            bounded_events.append(event)
            continue
        header = {
            "event_id": event.get("event_id", ""), "seq": event.get("seq", 0),
            "event_type": event.get("event_type", event.get("type", "")),
            "source_is_partial": True, "excerpt_index": 0,
            "excerpt_total": len(text), "payload_excerpt": "",
        }
        overhead = len(json.dumps(header, ensure_ascii=False, default=str)) + 2
        if overhead + 2 > budget:
            raise SnapshotValidationError(("event identity exceeds compaction chunk budget",))
        # Serialized source text can need another escaping layer; reserve its
        # worst-case doubling so each individual event also obeys the limit.
        width = max(1, (budget - overhead) // 2)
        total = (len(text) + width - 1) // width
        for index in range(total):
            bounded_events.append({
                **header, "excerpt_index": index, "excerpt_total": total,
                "payload_excerpt": text[index * width:(index + 1) * width],
            })
    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 2  # JSON list delimiters.
    for event in bounded_events:
        event_size = len(json.dumps(event, ensure_ascii=False, default=str))
        separator = 2 if current else 0
        if current and size + separator + event_size > budget:
            chunks.append(current)
            current, size, separator = [], 2, 0
        current.append(event)
        size += separator + event_size
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
    anchor = " ".join(str(value or "").lower().split())
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
