from __future__ import annotations

import os
from typing import Any


class RerankerProvider:
    """Cross-encoder reranker for Security RAG candidates."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        use_fp16: bool = True,
        max_chars: int = 1200,
    ) -> None:
        os.environ.setdefault("USE_TF", "0")
        os.environ.setdefault("TRANSFORMERS_NO_TF", "1")
        os.environ.setdefault("USE_FLAX", "0")
        os.environ.setdefault("TRANSFORMERS_NO_FLAX", "1")
        try:
            from FlagEmbedding import FlagReranker
        except ImportError as exc:
            raise RuntimeError(
                "FlagEmbedding is required when SECURITY_RAG_RERANKER_ENABLED=1. "
                "Install requirements.txt or disable the reranker."
            ) from exc

        self.model_name = model_name
        self.max_chars = max(128, int(max_chars or 1200))
        self._model = FlagReranker(model_name, use_fp16=bool(use_fp16))

    def rerank(self, query: str, candidates: list[Any]) -> list[tuple[float, Any]]:
        if not candidates:
            return []
        pairs = [
            (query, str(getattr(candidate, "text", "") or "")[: self.max_chars])
            for candidate in candidates
        ]
        scores = self._model.compute_score(pairs, normalize=True)
        if not isinstance(scores, list):
            scores = [scores]
        scored = [
            (float(score), candidate)
            for score, candidate in zip(scores, candidates)
        ]
        return sorted(scored, key=lambda item: item[0], reverse=True)
