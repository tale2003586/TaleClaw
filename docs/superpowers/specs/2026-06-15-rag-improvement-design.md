# Security RAG Improvement Design

**Date**: 2026-06-15
**Status**: Approved
**Scope**: Chunking, retrieval pipeline, router, reranker, evaluation, observability, incremental ingestion

## Context

当前 Security RAG 系统的问题诊断：
- **检索不相关是最核心的痛点**
- 14 万 chunks，基于 `BAAI/bge-m3`（1024d，已切换完成）
- 6.6 GB 知识库：339K JSON advisories + 2K Semgrep YAML + markdown 文档
- 现有评估指标（substring term match）不可信，需重构

## Architecture Overview

```
                         ┌───────────────────────┐
                         │     Router (改进)      │
                         │ keyword→embedding→LLM  │
                         └───────────┬───────────┘
                                     │ decision + rewritten query
                                     ▼
┌──────────────────────────────────────────────────────────────┐
│                    Qdrant Collection                          │
│  ┌─────────────────┐  ┌────────────────┐  ┌──────────────┐  │
│  │ dense (1024d)   │  │ sparse (lexical)│  │ payload idx  │  │
│  │ bge-m3          │  │ bge-m3         │  │ source_type   │  │
│  │                 │  │                │  │ severity      │  │
│  │                 │  │                │  │ language      │  │
│  └────────┬────────┘  └───────┬────────┘  └──────┬───────┘  │
│           └───────────────────┼──────────────────┘           │
│                               │ RRF Fusion                   │
│                               ▼                              │
│                        top-N candidates                      │
└───────────────────────────────┬──────────────────────────────┘
                                │ top-20~30
                                ▼
                    ┌───────────────────────┐
                    │  Reranker             │
                    │  bge-reranker-v2-m3   │
                    │  Cross-Encoder        │
                    └───────────┬───────────┘
                                │ reranked top-5
                                ▼
                         Context Injection
```

---

## Module 1: Chunking Strategy Refactoring

### 1.1 ChunkingStrategy Protocol

```python
class ChunkingStrategy(Protocol):
    """Protocol for source-type-specific chunking strategies."""
    @property
    def source_type(self) -> str: ...

    def chunk(self, path: Path, root: Path) -> list[KnowledgeChunk]: ...
    def supports(self, path: Path) -> bool: ...
```

### 1.2 Strategy A: JsonAdvisoryChunking

**Target**: 339K files under `advisory-database/advisories/` (`.json`)
**Problem**: Current 1800-char fixed window breaks JSON field integrity.

**Logic**:

```python
class JsonAdvisoryChunking:
    source_type = "advisory"

    def chunk(self, path, root):
        data = json.loads(path.read_text())
        chunks = []

        # Header block — prepended to EVERY chunk for traceability
        header = self._build_header(data)
        # "CVE/GHSA Advisory | GHSA-xxxx | CVE-2023-xxxx | Severity: HIGH | Package: flask"

        # Chunk 1: Summary (never split)
        summary = data.get("summary", "") or data.get("description", "")[:600]
        if summary:
            chunks.append(self._make_chunk(header + "\n\n" + summary, ...))

        # Chunk 2+: Description by paragraph
        desc = data.get("description", "")
        paragraphs = self._split_paragraphs(desc, max_chars=2000)
        for para in paragraphs:
            chunks.append(self._make_chunk(header + "\n\n" + para, ...))

        # Chunk N: References + patches
        refs = self._format_references(data.get("references", []))
        if refs:
            chunks.append(self._make_chunk(header + "\n\nReferences:\n" + refs, ...))

        return chunks

    def _build_header(self, data):
        return (
            f"CVE/GHSA Advisory\n"
            f"ID: {data.get('id', '')}\n"
            f"Aliases: {', '.join(data.get('aliases', []))}\n"
            f"Severity: {data.get('severity', 'UNKNOWN')}\n"
            f"Published: {data.get('published', '')}\n"
            f"Package: {self._affected_packages(data)}\n"
        )
```

**Payload metadata**:
```json
{
  "source_type": "advisory",
  "advisory_id": "GHSA-xxxx",
  "severity": "HIGH",
  "cwe": ["CWE-89"],
  "package": "flask",
  "language": "en"
}
```

### 1.3 Strategy B: SemgrepYamlChunking

**Target**: 2K files under `semgrep-rules/` (`.yaml`)
**Problem**: Current split ignores `rules` array boundary.

**Logic**:

```python
class SemgrepYamlChunking:
    source_type = "semgrep_rule"

    def chunk(self, path, root):
        docs = list(yaml.safe_load_all(path.read_text()))
        chunks = []
        rules = self._extract_rules(docs)

        for rule in rules:
            header = (
                f"Semgrep Rule\n"
                f"Rule ID: {rule.get('id', '')}\n"
                f"Severity: {rule.get('severity', 'UNKNOWN')}\n"
                f"Language: {rule.get('languages', [])}\n"
                f"CWE: {rule.get('metadata', {}).get('cwe', '')}\n"
                f"Category: {rule.get('metadata', {}).get('category', '')}\n"
            )
            message = rule.get("message", "")
            body = header + "\n\n" + message

            # Split patterns only when > 2000 chars
            if len(body) <= 2000:
                chunks.append(self._make_chunk(body, ...))
            else:
                sub_chunks = self._split_by_pattern_boundary(rule, header)
                chunks.extend(sub_chunks)

        return chunks
```

**Payload metadata**:
```json
{
  "source_type": "semgrep_rule",
  "rule_id": "java.lang.security.audit.xxx",
  "cwe": "CWE-89",
  "language": "java",
  "severity": "WARNING"
}
```

### 1.4 Strategy C: MarkdownDocChunking (improved existing)

**Improvements over current**:
1. **Heading path**: `H1 > H2 > H3` full hierarchy instead of just immediate heading
2. **Code block protection**: Detect ``` ```python ``` boundaries, never split inside code blocks
3. **List/table preservation**: Keep list items and table rows in the same chunk when possible
4. **Semantic overlap**: Overlap starts at nearest `\n\n` boundary instead of fixed offset

```python
class MarkdownDocChunking:
    source_type = "markdown"

    def chunk(self, path, root):
        text = read_text_file(path)
        heading_stack = []   # Track H1→H2→H3 path
        chunks = []

        for heading_level, heading_text, body, section_start in self._iter_sections(text):
            # Update heading stack
            heading_stack = heading_stack[:heading_level-1] + [heading_text]
            full_heading = " > ".join(heading_stack)

            # Split body by paragraph, respecting code blocks
            for sub_text, start, end in self._semantic_split(
                body, chunk_chars=1800, overlap_chars=200
            ):
                chunks.append(self._make_chunk(
                    render(full_heading, relpath, sub_text), ...
                ))
        return chunks

    def _semantic_split(self, text, chunk_chars, overlap_chars):
        """Split at paragraph/paragraph+code-block boundaries only."""
        ...
```

### 1.5 ChunkingRouter

```python
class ChunkingRouter:
    STRATEGIES = [JsonAdvisoryChunking, SemgrepYamlChunking, MarkdownDocChunking]
    DEFAULT = MarkdownDocChunking

    def strategy_for(self, path: Path) -> ChunkingStrategy:
        for strat_cls in self.STRATEGIES:
            strat = strat_cls()
            if strat.supports(path):
                return strat
        return self.DEFAULT()
```

### 1.6 Stable chunk ID

Keep existing `chunk_stable_id(path, start, end, text[:120])` SHA1 logic. Add `strategy_version` prefix for migration tracking:

```python
CHUNKING_VERSION = 2

def chunk_stable_id(path, start, end, text):
    digest = sha1(f"v{CHUNKING_VERSION}:{path}:{start}:{end}:{text[:120]}".encode())
    return f"security-kb:{digest}"
```

---

## Module 2: Hybrid Search

### 2.1 Qdrant Collection Setup (dual vectors)

```python
def ensure_collection(self, *, distance="Cosine"):
    if self.collection_exists():
        return
    self._client.create_collection(
        collection_name=self.collection,
        vectors_config={
            "dense": VectorParams(size=1024, distance=Distance.COSINE),
        },
        sparse_vectors_config={
            "sparse": SparseVectorParams(),
        },
    )
```

### 2.2 Embedding Provider Interface (post bge-m3 upgrade)

```python
class EmbeddingProvider(Protocol):
    vector_size: int  # 1024

    def embed_dense(self, text: str) -> list[float]: ...
    def embed_sparse(self, text: str) -> SparseVector: ...
```

### 2.3 Upsert with Sparse

```python
def upsert_chunks(self, chunks, *, batch_size=64):
    for batch in _batches(chunks, batch_size):
        points = []
        for chunk in batch:
            points.append(PointStruct(
                id=_point_id(chunk.id),
                vector={
                    "dense": self.embeddings.embed_dense(chunk.text),
                    "sparse": self.embeddings.embed_sparse(chunk.text),
                },
                payload=self._build_payload(chunk),
            ))
        self._client.upsert(collection_name=self.collection, points=points)
```

### 2.4 Hybrid Search with RRF

```python
def search(self, query, *, top_k=5, min_score=0.0, filter=None):
    dense_vec = self.embeddings.embed_dense(query)
    sparse_vec = self.embeddings.embed_sparse(query)

    # Reciprocal Rank Fusion — auto-calibrated
    results = self._client.query_points(
        collection_name=self.collection,
        prefetch=[
            Prefetch(
                query=dense_vec,
                using="dense",
                limit=max(20, top_k * 4),
            ),
            Prefetch(
                query=sparse_vec.as_object(),
                using="sparse",
                limit=max(20, top_k * 4),
            ),
        ],
        query=Fusion.RRF,
        limit=max(20, top_k * 4),   # enough for reranker
        query_filter=filter,          # optional payload filter
        with_payload=True,
    )
    return self._to_hits(results)
```

### 2.5 Payload Filtering

```python
def search_with_filter(self, query, source_type=None, severity=None, language=None):
    conditions = []
    if source_type:
        conditions.append(FieldCondition(
            key="source_type", match=MatchValue(value=source_type)
        ))
    if severity:
        conditions.append(FieldCondition(
            key="severity", match=MatchAny(any=list(severity))
        ))
    if language:
        conditions.append(FieldCondition(
            key="language", match=MatchValue(value=language)
        ))
    return self.search(query, filter=Filter(must=conditions) if conditions else None)
```

---

## Module 3: Reranker

### 3.1 Model

`BAAI/bge-reranker-v2-m3` — Cross-Encoder, ~1.5 GB VRAM, 中英双语.

### 3.2 Integration

```python
class RerankerProvider:
    def __init__(self, model_name="BAAI/bge-reranker-v2-m3"):
        from FlagEmbedding import FlagReranker
        self._model = FlagReranker(model_name, use_fp16=True)

    def rerank(self, query: str, candidates: list[KnowledgeHit]) -> list[tuple[float, KnowledgeHit]]:
        pairs = [(query, hit.text) for hit in candidates]
        scores = self._model.compute_score(pairs, normalize=True)  # list[float]
        return sorted(zip(scores, candidates), key=lambda x: x[0], reverse=True)
```

### 3.3 Pipeline

```python
def search(self, query, *, top_k=5, min_score=0.0, use_reranker=True):
    # Step 1: Hybrid retrieval (RRF) → top-30
    candidates = self._hybrid_search(query, limit=30)

    if not use_reranker or not self.reranker:
        return candidates[:top_k]

    # Step 2: Rerank
    scored = self.reranker.rerank(query, candidates)

    # Step 3: Threshold + truncate
    return [
        hit for score, hit in scored
        if score >= min_score
    ][:top_k]
```

---

## Module 4: Router Improvement

### 4.1 Query Rewriting Fix

**Current problem**: `rewrite_query()` concatenates ALL keyword expansions → 15+ keyword noise vector.

**Fix**:

```python
def rewrite_query(self, query, *, keyword_matches=None, matched_intent="", intent_score=0.0):
    # Rule 1: Exact keyword match → use the FIRST (most specific) expansion only
    if keyword_matches:
        best = keyword_matches[0]
        expansion = self.keywords.get(best, "")
        if expansion:
            return f"{query} {expansion}"

    # Rule 2: High-confidence intent match → append intent text
    if matched_intent and intent_score >= 0.80:
        return f"{query} {matched_intent}"

    # Rule 3: Low confidence → return original query, no rewriting
    return query
```

### 4.2 LLM Classifier as injected dependency

Move LLM classifier from `route_security_rag.py:build_llm_classifier` into `SecurityRetrievalRouter` as optional `LLMRouteClassifier` callable, same interface as current `Protocol`.

---

## Module 5: Evaluation Framework Redesign

### 5.1 Problems with Current Eval

| Issue | Detail |
|-------|--------|
| Term match as proxy | `term_hit_count` checks substring in hit text — no semantic judgment |
| No ranking metrics | Only checks top-1 `top_score`, no MRR/NDCG |
| No recall measurement | Can't tell if the right content is in top-K at all |
| No latency tracking | No per-query timing |
| Tiny testset | 24 cases, 6 negative — too few for statistical confidence |
| No error analysis | Can't see WHY a query failed |

### 5.2 New Testset Format

```jsonl
{
  "id": "sec-rag-001",
  "query": "SQL injection prevention prepared statements parameterized queries",
  "language": "en",
  "category": "sql_injection",
  "should_use_rag": true,
  "relevant_chunk_ids": ["security-kb:abc123", "security-kb:def456"],
  "irrelevant_chunk_ids": ["security-kb:xxx111"],
  "min_relevant_hits_in_top5": 2,
  "notes": "Should retrieve SQL injection prevention content."
}
```

### 5.3 Metrics

```python
@dataclass
class EvalMetrics:
    # Routing
    route_accuracy: float          # route_ok / total
    route_precision: float         # TP / (TP + FP)
    route_recall: float            # TP / (TP + FN)

    # Retrieval
    precision_at_k: dict[int, float]   # {1: 0.8, 3: 0.6, 5: 0.5}
    recall_at_k: dict[int, float]      # {5: 0.7, 10: 0.85}
    mrr: float                         # Mean Reciprocal Rank
    ndcg_at_k: dict[int, float]        # {5: 0.65, 10: 0.72}

    # Quality
    hit_rate: float                    # fraction of queries with ≥1 relevant hit
    first_relevant_rank_mean: float    # average position of first relevant hit

    # Performance
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
```

### 5.4 Evaluation Script (new)

```python
# scripts/eval_security_rag_v2.py
# - Loads new testset with relevant_chunk_ids
# - Runs router + hybrid search + reranker
# - Computes precision@k, recall@k, MRR, NDCG
# - Outputs per-query + aggregate JSON report
# - Supports --compare to diff two pipeline configs
```

### 5.5 Labeling utility

```python
# scripts/label_rag_relevance.py
# Interactive tool: given a query, show top-10 hits, user marks relevant/irrelevant
# Outputs to testset in the new format
```

---

## Module 6: Observability

### 6.1 Trace Format

```python
@dataclass
class RagTrace:
    trace_id: str
    timestamp: str
    query: str
    router_decision: RetrievalDecision
    hybrid_candidates: list[KnowledgeHit]   # top-30 pre-rerank
    reranked: list[tuple[float, str]]       # (score, chunk_id)
    final_hits: list[KnowledgeHit]          # top-5 delivered
    latency_ms: dict                        # {router: 12, hybrid_search: 45, rerank: 120}
    user_feedback: str | None = None        # "relevant" / "irrelevant" / None
```

### 6.2 Storage

Write to `rag_traces.jsonl` (rotated by day) in `~/.claude/rag_traces/`. Each trace ~2 KB, 100 queries/day ≈ 200 KB/day.

### 6.3 Dashboard script

```python
# scripts/rag_trace_stats.py
# Reads rag_traces.jsonl, prints:
#   - Query volume by route
#   - Avg latency per stage
#   - Reranker score distribution
#   - User feedback summary
```

---

## Module 7: Incremental Ingestion

### 7.1 Design

```python
class IncrementalIngester:
    def __init__(self, index, source_root, state_path):
        self.state = self._load_state(state_path)   # {path: mtime}
        self.source_root = source_root

    def sync(self):
        current_files = set(iter_source_files(self.source_root))
        current_paths = {str(f) for f in current_files}

        # Deletions: paths in state but not on disk
        deleted = set(self.state.keys()) - current_paths
        self._delete_chunks(deleted)

        # New/modified: paths on disk with different mtime
        for path in current_files:
            key = str(path)
            new_mtime = path.stat().st_mtime
            if key not in self.state or self.state[key] < new_mtime:
                chunks = chunks_from_file(path, root=self.source_root)
                self._delete_file_chunks(key)
                self.index.upsert_chunks(chunks)
                self.state[key] = new_mtime

        self._save_state()
```

### 7.2 Integration

- Add `--incremental` flag to `ingest_security_kb.py`
- Cron-friendly: `python scripts/ingest_security_kb.py --incremental`
- State file: `~/.claude/rag_ingest_state.json`

---

## Module 8: Caching Layer

### 8.1 Design

```python
class CachedSecurityIndex:
    """TTL cache wrapper around SecurityKnowledgeIndex."""

    def __init__(self, index, *, max_size=512, ttl_seconds=3600):
        self._index = index
        self._cache = LRUCache(max_size)
        self._ttl = ttl_seconds

    def search(self, query, **kwargs):
        cache_key = hashlib.sha256(
            json.dumps({"q": query, **kwargs}, sort_keys=True).encode()
        ).hexdigest()

        entry = self._cache.get(cache_key)
        if entry and (time.monotonic() - entry["ts"]) < self._ttl:
            return entry["hits"]

        hits = self._index.search(query, **kwargs)
        self._cache[cache_key] = {"hits": hits, "ts": time.monotonic()}
        return hits
```

---

## Module 9: A/B Testing Framework

### 9.1 Design

```python
@dataclass
class AbConfig:
    variant_a: SecurityKnowledgeIndex   # control (current)
    variant_b: SecurityKnowledgeIndex   # treatment (new)
    split_ratio: float = 0.5            # traffic to B

class AbRouter:
    def route(self, session_id: str) -> SecurityKnowledgeIndex:
        bucket = hash(session_id) % 100
        if bucket < int(self.split_ratio * 100):
            return self.variant_b
        return self.variant_a
```

---

## File Structure (post-implementation)

```
knowledge/
├── security_rag.py              # SecurityKnowledgeIndex (hybrid search + reranker)
├── chunking/
│   ├── __init__.py              # ChunkingRouter
│   ├── base.py                  # ChunkingStrategy Protocol
│   ├── advisory.py              # JsonAdvisoryChunking
│   ├── semgrep.py               # SemgrepYamlChunking
│   ├── markdown.py              # MarkdownDocChunking (improved)
│   └── utils.py                 # split_text, normalize_text, chunk_stable_id
├── reranker.py                  # RerankerProvider
├── caching.py                   # CachedSecurityIndex
└── incremental.py               # IncrementalIngester

retrieval/
├── __init__.py
├── security_router.py           # SecurityRetrievalRouter (improved rewrite_query)
└── ab_testing.py                # AbConfig, AbRouter

scripts/
├── ingest_security_kb.py        # Updated: new chunking strategies, --incremental
├── eval_security_rag_v2.py      # New eval with ranking metrics
├── label_rag_relevance.py       # New: interactive labeling tool
├── rag_trace_stats.py           # New: trace dashboard
├── search_security_kb.py        # Unchanged (uses new index internally)
├── security_rag_ask.py          # Unchanged
└── route_security_rag.py        # Unchanged

memory/
└── embeddings.py                # Updated: dense + sparse interface
```

---

## Data Migration

When chunking logic changes, existing vectors in Qdrant become stale.

**Strategy**: Use collection versioning.

```bash
# Old: code_security_kb
# New: code_security_kb_v2
SECURITY_RAG_COLLECTION=code_security_kb_v2 python scripts/ingest_security_kb.py --recreate
# Then update .env:
# SECURITY_RAG_COLLECTION=code_security_kb_v2
# Delete old collection when verified:
# curl -X DELETE http://localhost:6333/collections/code_security_kb
```

## Risks

| Risk | Mitigation |
|------|------------|
| Re-ingestion of 140K chunks takes long | Chunking is CPU-bound; 339K advisory JSONs parse fast; use multiprocessing for encode if needed |
| Reranker latency added to each search | Only rerank top-20~30; keep reranker model loaded once as singleton |
| Sparse vector storage overhead in Qdrant | Measured ~30% extra storage; acceptable on local disk |
| Collection migration downtime | Use old collection as fallback during verification phase |
