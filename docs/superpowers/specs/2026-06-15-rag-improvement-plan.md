# RAG Improvement Work Plan

**Date**: 2026-06-15
**Design doc**: [2026-06-15-rag-improvement-design.md](2026-06-15-rag-improvement-design.md)

## Phase Summary

| Phase | Scope | Est. effort | Priority |
|-------|-------|-------------|----------|
| P0: Chunking | 3 new strategies + chunking router | 2-3 days | P0 — core quality gate |
| P1: Hybrid Search | Qdrant dual vectors + RRF fusion | 1 day | P0 — core quality gate |
| P2: Reranker | Cross-Encoder integration | 1 day | P0 — core quality gate |
| P3: Router Fix | Query rewriting fix | 0.5 day | P0 — simple but high impact |
| P4: Evaluation V2 | New testset format + ranking metrics + labeling tool | 1.5 days | P1 — guides iteration |
| P5: Re-index | Full re-ingestion with new chunking + hybrid vectors | 1 day (mostly wait) | P1 — depends on P0-P4 |
| P6: Observability | Tracing + latency tracking + dashboard | 1.5 days | P1 — enables feedback loop |
| P7: Incremental Ingestion | File watcher + state file + --incremental flag | 1 day | P2 — production quality |
| P8: Caching | LRU + TTL cache wrapper | 0.5 day | P2 — nice to have |
| P9: A/B Testing | Split-router + trace comparison | 1 day | P2 — future iteration |

---

## P0: Chunking Strategy Refactoring

**Files to create**:
- [x] `knowledge/chunking/__init__.py` — ChunkingRouter, public API
- [x] `knowledge/chunking/base.py` — ChunkingStrategy Protocol, KnowledgeChunk dataclass
- [x] `knowledge/chunking/advisory.py` — JsonAdvisoryChunking
- [x] `knowledge/chunking/semgrep.py` — SemgrepYamlChunking
- [x] `knowledge/chunking/markdown.py` — MarkdownDocChunking (improved)
- [x] `knowledge/chunking/utils.py` — split_text, normalize_text, chunk_stable_id, read_text_file, etc.

**Files to modify**:
- [x] `knowledge/security_rag.py` — delegate `chunks_from_file` to `ChunkingRouter`; legacy helper path retained temporarily for compatibility
- [x] `tests/test_chunking.py` — unit tests per strategy

**Acceptance criteria**:
1. `JsonAdvisoryChunking`: chunk a sample advisory JSON; each chunk has header prepended; paragraphs are not broken mid-sentence
2. `SemgrepYamlChunking`: chunk a sample semgrep YAML; chunks align to rule boundaries
3. `MarkdownDocChunking`: chunk a markdown file with code blocks; code blocks are not split
4. `ChunkingRouter.supports()` correctly routes `.json` → advisory, `.yaml` → semgrep, `.md` → markdown
5. All existing tests pass
6. `python -c "from knowledge.chunking import ChunkingRouter"` loads cleanly

**Key decisions**:
- Advisory detection: check for `advisory-database` in path AND `.json` suffix AND JSON contains `id`/`aliases`/`severity` fields
- Semgrep detection: `.yaml`/`.yml` AND contains `rules` key at root level
- Fallback: MarkdownDocChunking works for all text files, handles `.rst`/`.txt` via same logic

---

## P1: Hybrid Search (Sparse + Dense)

**Files to modify**:
- [ ] `memory/embeddings.py` — add `embed_sparse()` to `FastEmbedProvider` (bge-m3 native sparse output)
- [ ] `knowledge/security_rag.py` — `ensure_collection` creates dual vectors; `upsert_chunks` writes both dense + sparse; `search` uses RRF fusion

**Acceptance criteria**:
1. `embed_sparse()` returns `SparseVector` with non-zero token weights
2. `ensure_collection()` creates collection with `vectors_config` + `sparse_vectors_config`
3. `upsert_chunks()` stores both vectors per point
4. `search()` uses `Fusion.RRF` with `Prefetch` for dense + sparse
5. Manual test: exact CVE ID query returns the advisory as #1 result (sparse path works)
6. Manual test: semantic query "SQL注入怎么防" returns relevant content (dense path works)

**Dependencies**: P0 chunking must be done first (payload schema changes).

---

## P2: Reranker

**Files to create**:
- [ ] `knowledge/reranker.py` — RerankerProvider, lazy-loaded singleton

**Files to modify**:
- [ ] `knowledge/security_rag.py` — `search()` accepts `use_reranker=True`; pipeline: hybrid → top-30 → rerank → top-5
- [ ] `runtime/bootstrap.py` — instantiate RerankerProvider, inject into SecurityKnowledgeIndex

**Acceptance criteria**:
1. `RerankerProvider` loads `BAAI/bge-reranker-v2-m3` successfully
2. `search(query, use_reranker=True)` returns hits with reranked order different from raw hybrid order
3. Reranker latency < 200ms for 20 candidates (expected: ~120ms on RTX 3060)
4. `search(query, use_reranker=False)` works unchanged (benchmark path)

---

## P3: Router Query Rewriting Fix

**Files to modify**:
- [x] `retrieval/security_router.py` — `rewrite_query()`: single keyword expansion only, intent expansion only when score ≥ 0.80, return original query otherwise

**Acceptance criteria**:
1. `rewrite_query("SQL injection ...", keyword_matches=["sql injection"])` → `"SQL injection ... SQL injection prevention prepared statements parameterized queries"` (one expansion, not all)
2. `rewrite_query("token", keyword_matches=["token", "jwt", "xss", "csrf"])` → `"token token storage leakage authentication security"` (first match only)
3. Existing eval testset: route precision must NOT degrade; recall may improve slightly
4. Visual check: rewritten queries are shorter and more focused

---

## P4: Evaluation Framework V2

**Files to create**:
- [ ] `benchmarks/security_rag_testset_v2.jsonl` — expanded testset (target: 50+ cases) with `relevant_chunk_ids`
- [x] `scripts/eval_security_rag_v2.py` — computes precision@k, recall@k, MRR, NDCG, latency percentiles
- [x] `scripts/label_rag_relevance.py` — interactive labeling CLI

**Acceptance criteria**:
1. `eval_security_rag_v2.py --testset benchmarks/security_rag_testset_v2.jsonl` outputs:
   - Per-query: precision@1/3/5, recall@5, MRR, latency
   - Aggregate: macro-averaged metrics
   - CSV/JSON output for comparison
2. `label_rag_relevance.py` accepts a query, runs search, shows top-10, waits for user to mark relevant chunks with y/n/s, writes to testset
3. Testset has at least 50 labeled queries (24 existing + 26 new, covering edge cases: empty result, exact CVE lookup, ambiguous bilingual queries)

---

## P5: Full Re-Index

**Procedure**:
1. Set `SECURITY_RAG_COLLECTION=code_security_kb_v2`
2. Run `python scripts/ingest_security_kb.py --source /home/tale/kaggle/code-security-kb --recreate --collection code_security_kb_v2`
3. Verify chunk count is reasonable (expected: fewer chunks than v1 due to structured chunking, but larger average chunk size)
4. Run `python scripts/eval_security_rag_v2.py` on v2 collection
5. Compare metrics vs baseline (run same eval on v1 collection)
6. If v2 metrics ≥ v1: update `.env` to `SECURITY_RAG_COLLECTION=code_security_kb_v2`
7. Keep `code_security_kb` for 1 week as rollback option

**Estimated runtime**: ~2-4 hours (339K advisory JSONs parse fast, embedding encode dominates)

---

## P6: Observability

**Files to create**:
- [x] `knowledge/tracing.py` — RagTrace dataclass, TraceStore (jsonl writer)
- [x] `scripts/rag_trace_stats.py` — aggregation dashboard

**Files to modify**:
- [ ] `knowledge/security_rag.py` — `search()` optionally accepts `trace_callback`
- [x] `runtime/context.py` — `_build_security_knowledge_block` emits trace after search
- [x] `plugins/security_rag/plugin.py` — `security_rag_search` emits trace after search

**Acceptance criteria**:
1. Every security RAG search writes one `RagTrace` to `rag_traces.jsonl`
2. `rag_trace_stats.py` prints: total queries, avg latency, route distribution, reranker score histogram
3. Traces are rotated daily; size is manageable (< 1 MB/day at typical usage)

---

## P7: Incremental Ingestion

**Files to create**:
- [ ] `knowledge/incremental.py` — IncrementalIngester with state file

**Files to modify**:
- [ ] `scripts/ingest_security_kb.py` — add `--incremental` flag

**Acceptance criteria**:
1. First run with `--incremental`: full index (no state yet)
2. Second run with `--incremental`: 0 chunks updated (state matches)
3. Touch a source file, re-run: only that file's chunks are updated
4. Delete a source file, re-run: that file's chunks are removed
5. State file is valid JSON, readable by human

---

## P8: Caching Layer

**Files to create**:
- [x] `knowledge/caching.py` — CachedSecurityIndex, LRU dict + TTL

**Files to modify**:
- [x] `knowledge/security_rag.py` — `build_security_index_from_env()` wraps the index with `CachedSecurityIndex` when enabled

**Acceptance criteria**:
1. Same query within TTL returns cached hits (no Qdrant call)
2. Different query → cache miss → Qdrant search
3. Cache is bounded (no memory leak)
4. `use_cache=False` bypass for benchmarks/evals

---

## P9: A/B Testing Framework

**Files to create**:
- [ ] `retrieval/ab_testing.py` — AbConfig, AbRouter

**Files to modify**:
- [ ] `runtime/bootstrap.py` — optionally build AbRouter when `SECURITY_RAG_AB_ENABLED=true`

**Acceptance criteria**:
1. `AbRouter` deterministically routes same session to same variant
2. Traces include `variant` tag when A/B is active
3. `rag_trace_stats.py --compare-a-b` shows side-by-side metrics

---

## Dependencies Map

```
P0 (Chunking) ──┬──→ P1 (Hybrid Search) ──→ P5 (Re-Index)
                │                                    │
                └──→ P3 (Router Fix) ────────────────┤
                                                     │
P2 (Reranker) ──────────────────────────────────────┤
                                                     │
                                          ┌──────────┘
                                          ▼
                                    P4 (Eval V2)
                                          │
                                    ┌─────┴─────┐
                                    ▼           ▼
                              P6 (Tracing)  P7 (Incremental)
                                    │           │
                                    └─────┬─────┘
                                          ▼
                                    P8 (Caching)
                                          │
                                          ▼
                                    P9 (A/B Testing)
```

P0→P1→P2→P3 can proceed in parallel once P0 chunking interface is defined.
P5 is the integration gate: all of P0-P4 must be done before re-index.

---

## Verification Checklist (per phase, before marking complete)

Each phase:
- [ ] Code passes `python -c "from module import *"`
- [ ] Code passes `ruff check`
- [ ] Relevant unit test passes
- [ ] Manual smoke test on 1-3 real queries
- [ ] git commit with Conventional Commits message

Gate before P5 (Re-Index):
- [ ] All P0-P4 code merged to branch
- [ ] Eval V2 runs on current v1 collection (baseline captured)
- [ ] `ingest_security_kb.py --help` shows new options
- [ ] `.env.example` updated with new config keys

---

## Implementation Status

**Status**: Core retrieval pipeline implemented on 2026-06-16. Full production validation still requires re-indexing with `SECURITY_RAG_HYBRID_ENABLED=1` and labeling a v2 relevance set.

### Completed

- **P0 Chunking**: Completed earlier. `chunks_from_file()` delegates to `ChunkingRouter`.
- **P1 Hybrid Search**: `SecurityKnowledgeIndex` can create dense+sparse Qdrant collections, upsert named dense/sparse vectors, and search with Qdrant RRF prefetch. Dense-only collections remain supported as fallback.
- **P2 Reranker**: Added `knowledge/reranker.py`; `search(..., use_reranker=True)` can rerank hybrid/dense candidates when `SECURITY_RAG_RERANKER_ENABLED=1`.
- **P3 Router Fix**: Completed earlier. Query rewriting uses focused keyword/intent expansion.
- **P4 Evaluation V2 tooling**: `eval_security_rag_v2.py` supports ranking metrics, reranker toggle, cache bypass, JSON/CSV output. `label_rag_relevance.py` exists for creating labeled records.
- **P6 Observability**: `search()` accepts `trace_callback`; context auto RAG and the `security_rag_search` tool now include internal search latency details in traces.
- **P7 Incremental Ingestion**: Added `knowledge/incremental.py` and `ingest_security_kb.py --incremental --state-file`.
- **P8 Caching**: Existing cache now ignores `trace_callback` in cache keys and supports `use_cache=False`.

### Remaining Manual Gates

- **P4 labeled testset**: `benchmarks/security_rag_testset_v2.jsonl` still needs real `relevant_chunk_ids`. These IDs must come from the freshly indexed collection; they should not be guessed.
- **P5 full re-index**: To actually use hybrid search, run ingestion into a new collection with `SECURITY_RAG_HYBRID_ENABLED=1` and `--recreate`.
- **P9 A/B testing**: Still intentionally deferred.

### Verification

- `python -m pytest tests/test_security_rag_hybrid.py tests/test_security_rag_incremental.py tests/test_security_rag_observability.py tests/test_security_rag_chunking.py -q`: passed
- `python -m py_compile memory/embeddings.py knowledge/security_rag.py knowledge/reranker.py knowledge/incremental.py scripts/ingest_security_kb.py scripts/eval_security_rag_v2.py scripts/label_rag_relevance.py scripts/rag_trace_stats.py`: passed
- `python scripts/ingest_security_kb.py --help`: shows `--incremental` and `--state-file`
