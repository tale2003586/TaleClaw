from __future__ import annotations

from dataclasses import dataclass, field
import json

from runtime.agent_spec import AgentSpec
from models.model_task_runner import ModelTaskRunner
from memory.candidates import MemoryCandidate
from memory.store import MemoryStore
from memory.vector_runtime import history_vector_scope_for_session


@dataclass
class MemoryProcessingResult:
    pending_added: int = 0
    candidates_updated: int = 0
    related_triggered: int = 0
    similar_hit_count: int = 0
    candidate_selected: bool = False
    candidate_save_result: str = ""
    similar_hits: list[dict] = field(default_factory=list)


class MemoryProcessingDevice:
    """Promote repeated user-described patterns from full history search into candidates."""

    def __init__(
        self,
        *,
        history_vector_index=None,
        scope_resolver=None,
        extractor=None,
        similar_top_k: int = 8,
        similar_min_score: float = 0.55,
        similar_min_hits: int = 2,
    ) -> None:
        self.history_vector_index = history_vector_index
        self.scope_resolver = scope_resolver or history_vector_scope_for_session
        self.extractor = extractor or CandidateMemoryExtractor()
        self.similar_top_k = max(1, int(similar_top_k))
        self.similar_min_score = float(similar_min_score)
        self.similar_min_hits = max(1, int(similar_min_hits))

    def process_user_description(
        self,
        *,
        store: MemoryStore,
        session,
        user_text: str,
        source_ref: str,
    ) -> MemoryProcessingResult:
        result = MemoryProcessingResult()
        text = user_text.strip()
        if not text or self.history_vector_index is None:
            return result

        hits = self._similar_history(session, text)
        result.similar_hit_count = len(hits)
        result.similar_hits = [
            {
                "id": hit.id,
                "score": hit.score,
                "source_type": hit.source_type,
                "source_ref": hit.source_ref,
                "message_count": (
                    hit.metadata.get("message_count")
                    if isinstance(hit.metadata, dict)
                    else None
                ),
            }
            for hit in hits
        ]
        if len(hits) < self.similar_min_hits:
            return result

        related = store.trigger_related_candidates(text, source_ref=source_ref)
        result.related_triggered += len(related)

        save_result = store.upsert_candidate(
            text,
            tag="history_pattern",
            confidence=self._confidence_from_hits(hits),
            source_ref=source_ref,
            metadata={
                "selection": {
                    "method": "history_vector_similarity",
                    "similar_min_hits": self.similar_min_hits,
                    "similar_min_score": self.similar_min_score,
                    "similar_hit_count": len(hits),
                },
                "similar_history": [
                    {
                        "id": hit.id,
                        "score": hit.score,
                        "source_type": hit.source_type,
                        "source_ref": hit.source_ref,
                        "message_count": (
                            hit.metadata.get("message_count")
                            if isinstance(hit.metadata, dict)
                            else None
                        ),
                    }
                    for hit in hits
                ],
            },
        )
        result.candidate_selected = True
        result.candidate_save_result = save_result
        if save_result.startswith("Saved"):
            result.pending_added += 1
        if save_result.startswith(("Saved", "Updated")):
            result.candidates_updated += 1
        return result

    def extract_stable_memory(self, candidate: MemoryCandidate) -> str:
        return self.extractor.extract(candidate).strip()

    def _similar_history(self, session, text: str):
        try:
            return self.history_vector_index.search(
                query=text,
                scope=self.scope_resolver(session),
                top_k=self.similar_top_k,
                min_score=self.similar_min_score,
            )
        except Exception:
            return []

    def _confidence_from_hits(self, hits: list) -> float:
        if not hits:
            return 0.5
        best = max(float(getattr(hit, "score", 0.0) or 0.0) for hit in hits)
        count_bonus = min(0.18, 0.04 * max(0, len(hits) - self.similar_min_hits))
        return min(0.9, max(0.55, best * 0.75 + count_bonus))


class CandidateMemoryExtractor:
    def __init__(
        self,
        *,
        runner: ModelTaskRunner | None = None,
        spec: AgentSpec | None = None,
        max_tokens: int = 220,
    ) -> None:
        self.runner = runner
        self.spec = spec or AgentSpec(
            name="candidate_memory_extractor",
            profile=None,
            model_purpose="summary",
            max_tokens=max_tokens,
        )
        self.max_tokens = max(1, int(max_tokens))

    def extract(self, candidate: MemoryCandidate) -> str:
        if self.runner is None:
            return candidate.content.strip()
        prompt = self._prompt(candidate)
        try:
            text = self.runner.run(
                spec=self.spec,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是记忆提取器。请从候选记忆和证据中提取一条稳定、可复用的用户偏好、"
                            "项目约定或长期事实。只输出应写入 MEMORY.md 的一句话或短段落。"
                            "不要输出解释、编号或寒暄。如果没有稳定记忆，输出空字符串。"
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=self.max_tokens,
            ).strip()
        except Exception:
            return candidate.content.strip()
        if text in {"无", "无稳定记忆", "没有稳定记忆", "空字符串"}:
            return ""
        return text

    def _prompt(self, candidate: MemoryCandidate) -> str:
        payload = {
            "candidate": candidate.content,
            "evidence_count": candidate.evidence_count,
            "confidence": candidate.confidence,
            "source_refs": candidate.source_refs,
            "metadata": candidate.metadata,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str)
