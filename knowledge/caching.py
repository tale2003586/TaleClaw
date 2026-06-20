from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
import time
from typing import Any


class CachedSecurityIndex:
    """Small in-process TTL cache wrapper around SecurityKnowledgeIndex."""

    def __init__(self, index: Any, *, max_size: int = 512, ttl_seconds: int = 3600) -> None:
        self._index = index
        self._max_size = max(1, int(max_size))
        self._ttl_seconds = max(0, int(ttl_seconds))
        self._cache: OrderedDict[str, dict] = OrderedDict()

    @property
    def collection(self) -> str:
        return self._index.collection

    def search(self, query: str, **kwargs):
        use_cache = kwargs.pop("use_cache", True)
        trace_callback = kwargs.get("trace_callback")
        cache_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key != "trace_callback"
        }
        if use_cache is False or self._ttl_seconds <= 0:
            return self._index.search(query, **kwargs)
        key = self._cache_key(query=query, kwargs=cache_kwargs)
        now = time.monotonic()
        entry = self._cache.get(key)
        if entry and now - entry["ts"] <= self._ttl_seconds:
            self._cache.move_to_end(key)
            if trace_callback is not None:
                trace_callback({
                    "cache_hit": True,
                    "final_count": len(entry["hits"]),
                    "latency_ms": {"cache": 0.0, "total": 0.0},
                })
            return entry["hits"]
        hits = self._index.search(query, **kwargs)
        self._cache[key] = {"ts": now, "hits": hits}
        self._cache.move_to_end(key)
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
        return hits

    def clear_cache(self) -> None:
        self._cache.clear()

    def __getattr__(self, name: str):
        return getattr(self._index, name)

    def _cache_key(self, *, query: str, kwargs: dict) -> str:
        payload = {"query": query, **kwargs}
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
