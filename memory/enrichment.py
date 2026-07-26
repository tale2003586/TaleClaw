from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

from memory.domain import MemoryKind, MemoryOwnerScope
from memory.governance import MemoryGovernancePipeline, MemoryWriteRequest
from memory.notes import MemoryNoteOrigin


MAX_DESCRIPTION_CHARS = 320
MAX_KEYWORDS = 12
MAX_TAGS = 8
MAX_VALUE_CHARS = 80


class EnrichmentAdapter(Protocol):
    def enrich(self, request: MemoryWriteRequest) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class MemoryEnrichment:
    contextual_description: str = ""
    keywords: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    memory_type: MemoryKind = MemoryKind.FACT
    confidence: float = 0.5
    origin: MemoryNoteOrigin = MemoryNoteOrigin.INFERRED_BY_LLM
    source: str = ""
    scope: MemoryOwnerScope = MemoryOwnerScope.USER
    scope_id: str = ""
    enrichment_version: int = 1

    def __post_init__(self) -> None:
        confidence = float(self.confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("enrichment confidence must be between 0 and 1.")
        if int(self.enrichment_version) < 1:
            raise ValueError("enrichment_version must be positive.")
        object.__setattr__(self, "contextual_description", _text(self.contextual_description, MAX_DESCRIPTION_CHARS))
        object.__setattr__(self, "keywords", _values(self.keywords, MAX_KEYWORDS))
        object.__setattr__(self, "tags", _values(self.tags, MAX_TAGS))
        object.__setattr__(self, "memory_type", MemoryKind(self.memory_type))
        object.__setattr__(self, "confidence", confidence)
        object.__setattr__(self, "origin", MemoryNoteOrigin(self.origin))
        object.__setattr__(self, "source", _text(self.source, 240))
        object.__setattr__(self, "scope", MemoryOwnerScope(self.scope))
        object.__setattr__(self, "scope_id", _text(self.scope_id, 160))
        object.__setattr__(self, "enrichment_version", int(self.enrichment_version))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in ("memory_type", "origin", "scope"):
            data[key] = getattr(self, key).value
        data["keywords"] = list(self.keywords)
        data["tags"] = list(self.tags)
        return data


@dataclass(frozen=True)
class MemoryEnrichmentResult:
    enrichment: MemoryEnrichment
    applied: bool
    fallback_used: bool
    audit: dict[str, Any] = field(default_factory=dict)


class DeterministicEnrichmentAdapter:
    """Local fallback that produces bounded hints and never calls a model."""

    def enrich(self, request: MemoryWriteRequest) -> Mapping[str, Any]:
        words = re.findall(r"[\w-]{3,}", request.content.lower(), flags=re.UNICODE)
        return {
            "contextual_description": request.content[:MAX_DESCRIPTION_CHARS],
            "keywords": list(dict.fromkeys(words))[:MAX_KEYWORDS],
            "tags": ["pending_enrichment"],
            "memory_type": request.metadata.get("memory_type", MemoryKind.FACT.value),
            "confidence": request.confidence,
        }


class PendingMemoryEnricher:
    def __init__(self, adapter: EnrichmentAdapter | None = None) -> None:
        self.adapter = adapter or DeterministicEnrichmentAdapter()
        self.governance = MemoryGovernancePipeline()

    def enrich(self, request: MemoryWriteRequest) -> MemoryEnrichmentResult:
        base = self._base(request)
        governed = self.governance.evaluate(request)
        if governed.classification.sensitive or governed.classification.prompt_injection:
            return MemoryEnrichmentResult(
                enrichment=base,
                applied=False,
                fallback_used=True,
                audit={
                    "status": "skipped_unsafe",
                    "classification": governed.audit.classification,
                    "enrichment_version": base.enrichment_version,
                },
            )
        try:
            raw = dict(self.adapter.enrich(request) or {})
            enriched = self._validated(request, raw)
            return MemoryEnrichmentResult(
                enrichment=enriched,
                applied=True,
                fallback_used=False,
                audit={
                    "status": "enriched",
                    "fields": sorted(raw.keys()),
                    "enrichment_version": enriched.enrichment_version,
                },
            )
        except Exception as exc:
            return MemoryEnrichmentResult(
                enrichment=base,
                applied=False,
                fallback_used=True,
                audit={
                    "status": "fallback",
                    "error_type": type(exc).__name__,
                    "enrichment_version": base.enrichment_version,
                },
            )

    def _base(self, request: MemoryWriteRequest) -> MemoryEnrichment:
        return MemoryEnrichment(
            confidence=request.confidence,
            origin=request.origin,
            source=request.source,
            scope=request.scope,
            scope_id=request.scope_id,
        )

    def _validated(self, request: MemoryWriteRequest, raw: dict[str, Any]) -> MemoryEnrichment:
        allowed = {
            "contextual_description", "keywords", "tags", "memory_type",
            "confidence", "enrichment_version",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"unsupported enrichment fields: {sorted(unknown)}")
        # Origin/source/scope are provenance. An adapter cannot rewrite or elevate them.
        return MemoryEnrichment(
            contextual_description=raw.get("contextual_description", ""),
            keywords=raw.get("keywords", ()),
            tags=raw.get("tags", ()),
            memory_type=raw.get("memory_type", MemoryKind.FACT),
            confidence=raw.get("confidence", request.confidence),
            origin=request.origin,
            source=request.source,
            scope=request.scope,
            scope_id=request.scope_id,
            enrichment_version=raw.get("enrichment_version", 1),
        )


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def _values(values: Any, limit: int) -> tuple[str, ...]:
    if isinstance(values, str):
        values = [values]
    return tuple(dict.fromkeys(
        _text(item, MAX_VALUE_CHARS)
        for item in values or ()
        if _text(item, MAX_VALUE_CHARS)
    ))[:limit]
