from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
from typing import Any

from memory.dedup import normalize_memory_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MemoryCandidate:
    id: str
    content: str
    type: str = "candidate"
    confidence: float = 0.5
    evidence_count: int = 1
    source_refs: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)
    updated_at: str = field(default_factory=_now_iso)
    last_triggered_at: str = field(default_factory=_now_iso)
    status: str = "candidate"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MemoryCandidate":
        return cls(
            id=str(payload.get("id") or ""),
            content=str(payload.get("content") or ""),
            type=str(payload.get("type") or "candidate"),
            confidence=float(payload.get("confidence") or 0.5),
            evidence_count=int(payload.get("evidence_count") or 1),
            source_refs=[
                str(item)
                for item in (payload.get("source_refs") or [])
                if str(item).strip()
            ],
            created_at=str(payload.get("created_at") or _now_iso()),
            updated_at=str(payload.get("updated_at") or _now_iso()),
            last_triggered_at=str(payload.get("last_triggered_at") or _now_iso()),
            status=str(payload.get("status") or "candidate"),
            metadata=dict(payload.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class CandidateMemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text(
                '{\n  "version": 1,\n  "candidates": []\n}\n',
                encoding="utf-8",
            )

    def read(self) -> list[MemoryCandidate]:
        with self._lock:
            return self._load_candidates_unlocked()

    def write(self, candidates: list[MemoryCandidate]) -> None:
        with self._lock:
            self._write_json_unlocked(candidates)

    def upsert(
        self,
        *,
        content: str,
        type: str = "candidate",
        confidence: float = 0.5,
        source_ref: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> tuple[MemoryCandidate, bool]:
        text = content.strip()
        now = _now_iso()
        with self._lock:
            candidates = self._load_candidates_unlocked()
            candidate = self._find_exact(candidates, text)
            if candidate is None:
                candidate = MemoryCandidate(
                    id=self._next_id(candidates),
                    content=text,
                    type=type,
                    confidence=max(0.0, min(1.0, float(confidence))),
                    evidence_count=1,
                    source_refs=[source_ref] if source_ref else [],
                    created_at=now,
                    updated_at=now,
                    last_triggered_at=now,
                    metadata=dict(metadata or {}),
                )
                candidates.append(candidate)
                created = True
            else:
                candidate.evidence_count += 1
                candidate.confidence = max(candidate.confidence, min(1.0, float(confidence)))
                candidate.confidence = min(1.0, candidate.confidence + 0.08)
                candidate.updated_at = now
                candidate.last_triggered_at = now
                if source_ref and source_ref not in candidate.source_refs:
                    candidate.source_refs.append(source_ref)
                candidate.metadata.update(metadata or {})
                created = False
            self._write_json_unlocked(candidates)
        return candidate, created

    def trigger_related(
        self,
        text: str,
        *,
        source_ref: str = "",
        min_overlap: int = 2,
    ) -> list[MemoryCandidate]:
        tokens = _tokens(text)
        normalized_text = normalize_memory_text(text)
        if not tokens and not normalized_text:
            return []
        with self._lock:
            candidates = self._load_candidates_unlocked()
            triggered = []
            now = _now_iso()
            for candidate in candidates:
                if candidate.status != "candidate":
                    continue
                overlap = tokens.intersection(_tokens(candidate.content))
                related_by_text = _text_related(normalized_text, candidate.content)
                if len(overlap) < min_overlap and not related_by_text:
                    continue
                candidate.evidence_count += 1
                candidate.confidence = min(1.0, candidate.confidence + 0.06)
                candidate.updated_at = now
                candidate.last_triggered_at = now
                if source_ref and source_ref not in candidate.source_refs:
                    candidate.source_refs.append(source_ref)
                triggered.append(candidate)
            if triggered:
                self._write_json_unlocked(candidates)
        return triggered

    def mark_promoted(self, ids: set[str]) -> None:
        now = _now_iso()
        with self._lock:
            candidates = self._load_candidates_unlocked()
            for candidate in candidates:
                if candidate.id in ids:
                    candidate.status = "promoted"
                    candidate.updated_at = now
            self._write_json_unlocked(candidates)

    def _find_exact(
        self,
        candidates: list[MemoryCandidate],
        content: str,
    ) -> MemoryCandidate | None:
        normalized = normalize_memory_text(content)
        for candidate in candidates:
            if normalize_memory_text(candidate.content) == normalized:
                return candidate
        return None

    def _load_candidates_unlocked(self) -> list[MemoryCandidate]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            payload = {}
        items = payload.get("candidates", []) if isinstance(payload, dict) else []
        candidates = [
            MemoryCandidate.from_dict(item)
            for item in items
            if isinstance(item, dict) and str(item.get("content") or "").strip()
        ]
        existing = {candidate.id for candidate in candidates if candidate.id}
        for candidate in candidates:
            if candidate.id:
                continue
            candidate.id = self._next_id(candidates, existing=existing)
            existing.add(candidate.id)
        return sorted(candidates, key=lambda item: (item.created_at, item.id))

    def _next_id(
        self,
        candidates: list[MemoryCandidate],
        *,
        existing: set[str] | None = None,
    ) -> str:
        existing_ids = existing or {candidate.id for candidate in candidates}
        index = len(existing_ids) + 1
        while True:
            candidate_id = f"mem_cand_{index:04d}"
            if candidate_id not in existing_ids:
                return candidate_id
            index += 1

    def _write_json_unlocked(self, candidates: list[MemoryCandidate]) -> None:
        payload = {
            "version": 1,
            "backend": "json",
            "candidates": [candidate.to_dict() for candidate in candidates],
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )


def _tokens(text: str) -> set[str]:
    normalized = normalize_memory_text(text)
    raw_tokens = normalized.replace("_", " ").replace("-", " ").split()
    tokens = {
        token
        for token in raw_tokens
        if len(token) >= 2 and token not in {"用户", "这个", "一个", "需要"}
    }
    for keyword in _memory_keywords():
        if keyword in normalized:
            tokens.add(keyword)
    return tokens


def _text_related(normalized_text: str, candidate_content: str) -> bool:
    normalized_candidate = normalize_memory_text(candidate_content)
    if not normalized_text or not normalized_candidate:
        return False
    if normalized_text in normalized_candidate or normalized_candidate in normalized_text:
        return True
    shared_keywords = [
        keyword
        for keyword in _memory_keywords()
        if keyword in normalized_text and keyword in normalized_candidate
    ]
    return len(shared_keywords) >= 1


_DEFAULT_MEMORY_KEYWORDS = {
    "pytest",
    "测试",
    "代码风格",
    "项目",
    "偏好",
    "喜欢",
    "不喜欢",
    "真实",
    "搜索",
    "人物细节",
    "同人文",
    "prefer",
    "preference",
}


def _memory_keywords() -> set[str]:
    raw = os.getenv("MEMORY_CANDIDATE_KEYWORDS", "").strip()
    if not raw:
        return set(_DEFAULT_MEMORY_KEYWORDS)
    configured = {
        item.strip().lower()
        for item in raw.split(",")
        if item.strip()
    }
    return configured | set(_DEFAULT_MEMORY_KEYWORDS)
