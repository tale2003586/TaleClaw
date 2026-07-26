from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from user_scope import user_id_for_session


@dataclass(frozen=True)
class EpisodicBoundary:
    user_id: str
    session_id: str | None = None
    application: str | None = None
    task_id: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.user_id or "").strip():
            raise ValueError("Episodic boundary requires user_id.")
        if not self.session_id and not (
            self.task_id or self.workspace_id or self.project_id
        ):
            raise ValueError(
                "Episodic boundary requires current session or trusted coding scope."
            )

    @classmethod
    def from_session(cls, session) -> "EpisodicBoundary":
        metadata = getattr(session, "metadata", {}) or {}
        is_coding = metadata.get("kind") == "coding_application"
        return cls(
            user_id=user_id_for_session(session),
            session_id=str(getattr(session, "id", "") or "") or None,
            application=("coding" if is_coding else metadata.get("application")),
            task_id=_text(metadata.get("task_id")) if is_coding else None,
            workspace_id=_text(metadata.get("workspace_id") or metadata.get("workspace_root")) if is_coding else None,
            project_id=_text(metadata.get("project_id") or metadata.get("repository")) if is_coding else None,
        )

    def filters(self) -> dict[str, str]:
        values = {
            "source_type": "session_turn",
            "metadata.user_id": self.user_id,
        }
        if self.task_id:
            values["metadata.task_id"] = self.task_id
        elif self.session_id:
            values["metadata.session_id"] = self.session_id
        else:
            raise ValueError("Unsafe episodic boundary.")
        if self.workspace_id:
            values["metadata.workspace_id"] = self.workspace_id
        if self.project_id:
            values["metadata.project_id"] = self.project_id
        return values


@dataclass(frozen=True)
class EpisodicHit:
    id: str
    text: str
    score: float
    source_ref: str
    session_id: str
    source_type: str = "session_turn"
    label: str = "past_event"
    metadata: dict | None = None


@dataclass(frozen=True)
class EpisodicResult:
    hits: tuple[EpisodicHit, ...] = ()
    boundary: EpisodicBoundary | None = None
    degraded: bool = False


class EpisodicHistoryRetrievalService:
    def __init__(
        self,
        index,
        *,
        top_k: int = 6,
        min_score: float = 0.35,
        trace: Callable[..., None] | None = None,
    ) -> None:
        self.index = index
        self.top_k = max(1, int(top_k))
        self.min_score = float(min_score)
        self.trace = trace
        self.trace_events: list[dict] = []

    def drain_trace_events(self) -> list[dict]:
        events = list(self.trace_events)
        self.trace_events.clear()
        return events

    def retrieve(self, query: str, boundary: EpisodicBoundary) -> EpisodicResult:
        if self.index is None or not str(query).strip():
            return EpisodicResult(boundary=boundary)
        try:
            if hasattr(self.index, "search_filtered"):
                raw_hits = self.index.search_filtered(
                    query=query,
                    filters=boundary.filters(),
                    top_k=self.top_k,
                    min_score=self.min_score,
                )
            elif boundary.session_id and hasattr(self.index, "search"):
                raw_hits = self.index.search(
                    query=query,
                    scope=f"session:{boundary.session_id}",
                    top_k=self.top_k,
                    min_score=self.min_score,
                )
            else:
                return EpisodicResult(boundary=boundary, degraded=True)
        except Exception:
            return EpisodicResult(boundary=boundary, degraded=True)
        hits = []
        for hit in raw_hits:
            metadata = hit.metadata if isinstance(getattr(hit, "metadata", None), dict) else {}
            session_id = str(metadata.get("session_id") or "")
            if boundary.task_id:
                if str(metadata.get("task_id") or "") != boundary.task_id:
                    continue
            elif session_id != boundary.session_id:
                continue
            hits.append(EpisodicHit(
                id=str(getattr(hit, "id", "")),
                text=str(getattr(hit, "text", "")),
                score=float(getattr(hit, "score", 0.0)),
                source_ref=str(getattr(hit, "source_ref", "")),
                session_id=session_id,
                metadata=dict(metadata),
            ))
        result = EpisodicResult(tuple(hits), boundary, False)
        self._emit(result)
        return result

    def render(self, result: EpisodicResult) -> str:
        if not result.hits:
            return ""
        lines = ["<episodic_history label=\"past_events\">"]
        for index, hit in enumerate(result.hits, start=1):
            lines.append(
                f"[{index}] past_event score={hit.score:.4f} "
                f"session_id={hit.session_id} source_ref={hit.source_ref}\n{hit.text.strip()}"
            )
        lines.append("</episodic_history>")
        return "\n\n".join(lines)

    def _emit(self, result: EpisodicResult) -> None:
        payload = {
            "hit_count": len(result.hits),
            "session_id": result.boundary.session_id if result.boundary else None,
            "task_id": result.boundary.task_id if result.boundary else None,
            "degraded": result.degraded,
        }
        self.trace_events.append({"event": "memory.episodic.retrieved", "payload": payload})
        if self.trace is None:
            return
        try:
            self.trace("memory.episodic.retrieved", payload)
        except TypeError:
            self.trace({"event": "memory.episodic.retrieved", "payload": payload})


def _text(value) -> str | None:
    text = str(value or "").strip()
    return text or None
