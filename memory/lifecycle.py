from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json

from memory.archive_store import ArchivedRecentTurn, MemoryArchiveStore
from memory.history_summary import HistorySummarizer
from memory.processor import MemoryProcessingDevice
from memory.store import MemoryStore
from memory.vector_index import MemoryRecord
from memory.vector_runtime import history_vector_scope_for_session


@dataclass
class MemoryLifecycleResult:
    pending_added: int = 0
    candidates_updated: int = 0
    related_triggered: int = 0
    promoted_count: int = 0
    vector_indexed: int = 0
    vector_errors: int = 0
    history_updated: bool = False
    recent_context_updated: bool = False
    archived_count: int = 0
    trace_events: list[dict] = field(default_factory=list)

    def to_trace_payload(self) -> dict:
        return {
            "pending_added": self.pending_added,
            "candidates_updated": self.candidates_updated,
            "related_triggered": self.related_triggered,
            "promoted_count": self.promoted_count,
            "vector_indexed": self.vector_indexed,
            "vector_errors": self.vector_errors,
            "history_updated": self.history_updated,
            "recent_context_updated": self.recent_context_updated,
            "archived_count": self.archived_count,
        }


class MemoryLifecycle:
    """Derived memory lifecycle layered on top of the raw session transcript."""

    def __init__(
        self,
        store: MemoryStore,
        *,
        summarizer: HistorySummarizer | None = None,
        archive_store: MemoryArchiveStore | None = None,
        history_vector_index=None,
        memory_processor: MemoryProcessingDevice | None = None,
        scope_resolver=None,
        recent_limit: int = 6,
        promotion_confidence: float = 0.85,
        promotion_evidence_count: int = 3,
    ) -> None:
        self.store = store
        self.summarizer = summarizer or HistorySummarizer()
        self.archive_store = archive_store
        self.history_vector_index = history_vector_index
        self.scope_resolver = scope_resolver or history_vector_scope_for_session
        self.memory_processor = memory_processor or MemoryProcessingDevice(
            history_vector_index=history_vector_index,
            scope_resolver=self.scope_resolver,
        )
        self.recent_limit = max(1, recent_limit)
        self.promotion_confidence = max(0.0, min(1.0, float(promotion_confidence)))
        self.promotion_evidence_count = max(1, int(promotion_evidence_count))

    def after_turn(self, session) -> MemoryLifecycleResult:
        store = self.store
        if hasattr(store, "for_session"):
            store = store.for_session(session)
        user_text = self._last_text(session.messages, "user")
        assistant_text = self._last_text(session.messages, "assistant")
        if not user_text and not assistant_text:
            return MemoryLifecycleResult()

        result = MemoryLifecycleResult()
        source_ref = f"{session.id}:{max(0, len(session.messages) - 1)}"

        explicit = self._extract_explicit_memory(user_text)
        if explicit:
            save_result = store.append("memory", explicit)
            if save_result.startswith("Saved"):
                result.pending_added += 1
        elif self._candidate_memory_enabled():
            processed = self.memory_processor.process_user_description(
                store=store,
                session=session,
                user_text=user_text,
                source_ref=source_ref,
            )
            result.pending_added += processed.pending_added
            result.candidates_updated += processed.candidates_updated
            result.related_triggered += processed.related_triggered
            result.trace_events.append({
                "event": "memory.candidate.evaluated",
                "payload": {
                    "source_ref": source_ref,
                    "method": "history_vector_similarity",
                    "similar_hit_count": processed.similar_hit_count,
                    "similar_hits": processed.similar_hits,
                    "candidate_selected": processed.candidate_selected,
                    "similar_min_hits": self.memory_processor.similar_min_hits,
                    "similar_min_score": self.memory_processor.similar_min_score,
                    "user_text_preview": _trim_preview(user_text),
                },
            })
            if processed.candidate_selected:
                result.trace_events.append({
                    "event": "memory.candidate.upserted",
                    "payload": {
                        "source_ref": source_ref,
                        "save_result": processed.candidate_save_result,
                        "pending_added": processed.pending_added,
                        "candidates_updated": processed.candidates_updated,
                        "related_triggered": processed.related_triggered,
                    },
                })
            promoted = self._promote_ready_candidates(store)
            result.promoted_count += len(promoted)
            for item in promoted:
                result.trace_events.append({
                    "event": "memory.candidate.promoted",
                    "payload": item,
                })

        assistant_summary = self.summarizer.summarize(assistant_text)
        history = self._format_history_entry(user_text, assistant_summary)
        if history:
            store.append_history(history, source_ref=source_ref)
            result.history_updated = True
        result.vector_indexed += self._index_session_turn(
            session,
            result,
            source_ref=source_ref,
            assistant_summary=assistant_summary,
        )
        result.vector_indexed += self._index_memory_files(
            store,
            session,
            result,
            source_ref=source_ref,
        )

        recent_turns = store.read_recent_turns()
        recent_turns.append(
            self._format_recent_turn(
                session,
                user_text,
                assistant_summary,
                source_ref=source_ref,
            )
        )
        evicted_turns = recent_turns[:-self.recent_limit]
        store.write_recent_turns(recent_turns[-self.recent_limit :])
        result.recent_context_updated = True

        if self.archive_store:
            for turn in evicted_turns:
                archived = self.archive_store.append(ArchivedRecentTurn(**turn))
                if archived:
                    result.archived_count += 1
        return result

    def _index_session_turn(
        self,
        session,
        result: MemoryLifecycleResult,
        *,
        source_ref: str,
        assistant_summary: str,
    ) -> int:
        turn_messages = self._last_turn_messages(session.messages)
        if self.history_vector_index is None or not turn_messages:
            return 0
        text = self._render_messages_for_embedding(turn_messages)
        if not text.strip():
            return 0
        try:
            self.history_vector_index.upsert(
                MemoryRecord(
                    id=f"session_turn:{source_ref}:{_text_digest(text)}",
                    text=text,
                    scope=self.scope_resolver(session),
                    source_type="session_turn",
                    source_ref=source_ref,
                    metadata={
                        "session_id": getattr(session, "id", ""),
                        "mode": getattr(session, "current_mode", ""),
                        "message_count": len(turn_messages),
                        "messages": turn_messages,
                        "assistant_summary": assistant_summary,
                        "created_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            )
            result.trace_events.append({
                "event": "memory.history_vector.upserted",
                "payload": {
                    "source_ref": source_ref,
                    "scope": self.scope_resolver(session),
                    "source_type": "session_turn",
                    "message_count": len(turn_messages),
                    "text_chars": len(text),
                },
            })
            return 1
        except Exception as exc:
            result.vector_errors += 1
            result.trace_events.append({
                "event": "memory.history_vector.failed",
                "payload": {
                    "source_ref": source_ref,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
            })
            return 0

    def _index_memory_files(
        self,
        store: MemoryStore,
        session,
        result: MemoryLifecycleResult,
        *,
        source_ref: str,
    ) -> int:
        if self.history_vector_index is None:
            return 0
        indexed = 0
        scope = self.scope_resolver(session)
        for section, path in [
            ("self", store.self_path),
            ("memory", store.memory_path),
            ("now", store.now_path),
            ("pending", store.pending_path),
            ("history", store.history_path),
        ]:
            try:
                text = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if not text or text in {f"# {section.title()}", "# Pending Memory", "# History"}:
                continue
            try:
                self.history_vector_index.upsert(
                    MemoryRecord(
                        id=f"memory_file:{scope}:{path.name}",
                        text=text,
                        scope=scope,
                        source_type="memory_file",
                        source_ref=str(path.name),
                        metadata={
                            "session_id": getattr(session, "id", ""),
                            "section": section,
                            "path": str(path),
                            "text_digest": _text_digest(text),
                            "text_chars": len(text),
                            "source_ref": source_ref,
                            "updated_at": datetime.now(timezone.utc).isoformat(),
                        },
                    )
                )
                indexed += 1
                result.trace_events.append({
                    "event": "memory.file_vector.upserted",
                    "payload": {
                        "source_ref": source_ref,
                        "scope": scope,
                        "source_type": "memory_file",
                        "section": section,
                        "path": path.name,
                        "text_chars": len(text),
                    },
                })
            except Exception as exc:
                result.vector_errors += 1
                result.trace_events.append({
                    "event": "memory.file_vector.failed",
                    "payload": {
                        "source_ref": source_ref,
                        "section": section,
                        "path": path.name,
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    },
                })
        return indexed

    def _last_turn_messages(self, messages: list[dict]) -> list[dict]:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if isinstance(message, dict) and message.get("role") == "user":
                return [self._json_safe_message(item) for item in messages[index:]]
        return [self._json_safe_message(item) for item in messages]

    def _json_safe_message(self, message) -> dict:
        if isinstance(message, dict):
            return json.loads(json.dumps(message, ensure_ascii=False, default=str))
        return {"role": "unknown", "content": str(message)}

    def _render_messages_for_embedding(self, messages: list[dict]) -> str:
        parts = []
        for message in messages:
            role = str(message.get("role") or "unknown")
            content = message.get("content")
            if content not in (None, ""):
                rendered = _text_for_embedding(content)
                if rendered:
                    parts.append(f"{role}: {rendered}")
            for tool_call in message.get("tool_calls") or []:
                rendered = _text_for_embedding(tool_call)
                if rendered:
                    parts.append(f"{role}.tool_call: {rendered}")
            if role == "tool":
                tool_call_id = message.get("tool_call_id", "")
                rendered = _text_for_embedding(content)
                if rendered:
                    parts.append(f"tool_result[{tool_call_id}]: {rendered}")
        return "\n".join(parts).strip()

    def _last_text(self, messages: list[dict], role: str) -> str:
        for message in reversed(messages):
            if message.get("role") != role:
                continue
            content = message.get("content", "")
            if isinstance(content, str):
                return content.strip()
        return ""

    def _extract_explicit_memory(self, text: str) -> str:
        markers = [
            "记住",
            "请记住",
            "帮我记住",
            "以后记得",
            "remember that",
            "please remember",
        ]
        lowered = text.lower()
        for marker in markers:
            idx = lowered.find(marker.lower())
            if idx >= 0:
                return text[idx + len(marker):].strip(" ：:，,。.\n")
        return ""

    def _candidate_memory_enabled(self) -> bool:
        return True

    def _promote_ready_candidates(self, store: MemoryStore):
        promoted = []
        promoted_ids = set()
        for candidate in store.candidates().read():
            if candidate.status != "candidate":
                continue
            if (
                candidate.confidence >= self.promotion_confidence
                or candidate.evidence_count >= self.promotion_evidence_count
            ):
                memory_text = self.memory_processor.extract_stable_memory(candidate)
                if not memory_text:
                    continue
                save_result = store.append("memory", memory_text)
                if save_result.startswith("Saved") or save_result.startswith("Duplicate"):
                    promoted.append({
                        "candidate_id": candidate.id,
                        "source_refs": list(candidate.source_refs),
                        "evidence_count": candidate.evidence_count,
                        "confidence": candidate.confidence,
                        "memory_text_preview": _trim_preview(memory_text),
                        "save_result": save_result,
                    })
                    promoted_ids.add(candidate.id)
        if promoted_ids:
            store.candidates().mark_promoted(promoted_ids)
        return promoted

    def _format_history_entry(self, user_text: str, assistant_summary: str) -> str:
        parts = []
        if user_text:
            parts.append(f"USER:\n{user_text}")
        if assistant_summary:
            parts.append(f"ASSISTANT_SUMMARY:\n{assistant_summary}")
        return "\n\n".join(parts)

    def _format_recent_turn(
        self,
        session,
        user_text: str,
        assistant_summary: str,
        *,
        source_ref: str,
    ) -> dict:
        return {
            "session_id": session.id,
            "mode": session.current_mode,
            "user_text": user_text,
            "assistant_summary": assistant_summary,
            "source_ref": source_ref,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "metadata": {},
        }

    def _trim(self, text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        return text[:limit].rstrip() + f"... ({len(text) - limit} chars omitted)"


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _text_for_embedding(value) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, str):
        parsed = _parse_json_text(value)
        if parsed is not None:
            rendered = _text_for_embedding(parsed)
            return rendered or value
        return value
    if isinstance(value, dict):
        parts = []
        for key in (
            "text",
            "content",
            "summary",
            "message",
            "error",
            "title",
            "description",
            "name",
        ):
            if key in value:
                rendered = _text_for_embedding(value.get(key))
                if rendered:
                    parts.append(rendered)
        function = value.get("function")
        if isinstance(function, dict):
            name = _text_for_embedding(function.get("name"))
            arguments = _text_for_embedding(function.get("arguments"))
            if name:
                parts.append(f"function={name}")
            if arguments:
                parts.append(f"arguments={arguments}")
        if parts:
            return "\n".join(dict.fromkeys(parts))
        nested = [
            _text_for_embedding(item)
            for item in value.values()
            if item not in (None, "")
        ]
        return "\n".join(item for item in nested if item)[:4000]
    if isinstance(value, list):
        parts = [_text_for_embedding(item) for item in value]
        return "\n".join(item for item in parts if item)
    return str(value)


def _parse_json_text(text: str):
    stripped = text.strip()
    if not stripped or stripped[0] not in "{[":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _trim_preview(text: str, limit: int = 500) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return value[:limit] + "...[truncated]"
