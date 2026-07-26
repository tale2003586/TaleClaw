from __future__ import annotations

from memory.dedup import normalize_memory_text
from memory.domain import MemoryItem, OwnerKey
from memory.semantic_index import IndexedMemoryHit


class InMemoryMemoryIndex:
    def __init__(self) -> None:
        self.items: dict[tuple[str, int], MemoryItem] = {}
        self.fail_upsert = False
        self.fail_delete = False
        self.fail_search = False
        self.search_calls: list[dict] = []

    def upsert(self, item: MemoryItem) -> None:
        if self.fail_upsert:
            raise RuntimeError("injected upsert failure")
        self.items[(item.id, item.version)] = item

    def delete(self, memory_id: str, version: int | None = None) -> None:
        if self.fail_delete:
            raise RuntimeError("injected delete failure")
        for key in list(self.items):
            if key[0] == memory_id and (version is None or key[1] == version):
                del self.items[key]

    def search(self, query: str, scopes: list[OwnerKey] | tuple[OwnerKey, ...], top_k: int):
        if self.fail_search:
            raise RuntimeError("injected search failure")
        self.search_calls.append({"query": query, "scopes": tuple(scopes), "top_k": top_k})
        allowed = set(scopes)
        terms = set(normalize_memory_text(query))
        hits = []
        for item in self.items.values():
            if item.owner not in allowed:
                continue
            candidate = set(item.normalized_content)
            score = len(terms & candidate) / max(1, len(terms | candidate))
            hits.append(IndexedMemoryHit(
                memory_id=item.id,
                memory_version=item.version,
                score=score,
                owner_scope=item.owner_scope.value,
                owner_id=item.owner_id,
                kind=item.kind.value,
            ))
        return sorted(hits, key=lambda hit: hit.score, reverse=True)[:max(1, int(top_k))]
