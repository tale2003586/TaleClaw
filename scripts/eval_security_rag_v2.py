from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_testset_v2.jsonl"
FALLBACK_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Evaluate Security RAG with ranking metrics.")
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--collection", default=None)
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--search-all", action="store_true")
    parser.add_argument("--llm-classifier", action="store_true", help="Use the LLM classifier for ambiguous routing.")
    parser.add_argument("--no-llm-classifier", action="store_true", help="Disable the LLM classifier for this run.")
    parser.add_argument("--no-reranker", action="store_true", help="Disable reranker for this eval run.")
    parser.add_argument("--no-cache", action="store_true", help="Bypass in-process RAG cache.")
    parser.add_argument("--json-out", default=None)
    parser.add_argument("--csv-out", default=None)
    args = parser.parse_args()

    testset = Path(args.testset)
    if args.testset == str(DEFAULT_TESTSET) and not testset.exists():
        testset = FALLBACK_TESTSET
    cases = load_cases(testset)
    router = build_security_retrieval_router_from_env()
    classifier = build_security_route_classifier_from_env(
        config=router.config,
        enabled=_llm_enabled(args),
    )
    index = build_security_index_from_env(collection=args.collection)

    rows = []
    for case in cases:
        started = time.perf_counter()
        decision = router.route(str(case["query"]), llm_classifier=classifier)
        route_ms = (time.perf_counter() - started) * 1000
        hits = []
        search_ms = 0.0
        if decision.use_rag or args.search_all:
            search_started = time.perf_counter()
            hits = index.search(
                decision.query,
                top_k=args.top_k,
                min_score=args.min_score if args.min_score is not None else decision.min_score,
                use_reranker=not args.no_reranker,
                use_cache=not args.no_cache,
            )
            search_ms = (time.perf_counter() - search_started) * 1000
        rows.append(evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=search_ms))

    report = {"summary": summarize(rows), "cases": rows}
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.csv_out:
        write_csv(Path(args.csv_out), rows)
    return 0


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = []
    with path.open("r", encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            item = json.loads(line)
            if "id" not in item or "query" not in item:
                raise SystemExit(f"Missing id/query at {path}:{line_number}")
            cases.append(item)
    return cases


def _llm_enabled(args) -> bool:
    if args.no_llm_classifier:
        return False
    if args.llm_classifier:
        return True
    return _env_bool("SECURITY_RAG_ROUTE_LLM_ENABLED", False)


def _env_bool(name: str, default: bool = False) -> bool:
    import os

    value = os.getenv(name)
    if value is None or value == "":
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def evaluate_case(case: dict[str, Any], decision: Any, hits: list[Any], *, route_ms: float, search_ms: float) -> dict[str, Any]:
    relevant_ids = set(str(item) for item in case.get("relevant_chunk_ids", []) if str(item).strip())
    irrelevant_ids = set(str(item) for item in case.get("irrelevant_chunk_ids", []) if str(item).strip())
    expected_terms = [str(term).lower() for term in case.get("expected_terms", []) if str(term).strip()]
    expected_source_hints = [str(hint).lower() for hint in case.get("expected_source_hints", []) if str(hint).strip()]
    hit_ids = [str(hit.id) for hit in hits]
    relevance = [1 if hit_id in relevant_ids else 0 for hit_id in hit_ids]
    has_labels = bool(relevant_ids)
    if not has_labels:
        blob_per_hit = [f"{hit.source_relpath}\n{hit.title}\n{hit.text}".lower() for hit in hits]
        relevance = [
            1 if (expected_terms and any(term in blob for term in expected_terms)) else 0
            for blob in blob_per_hit
        ]
    source_blob = "\n".join(f"{hit.source_relpath}\n{hit.title}" for hit in hits).lower()
    route_ok = bool(decision.use_rag) == bool(case.get("should_use_rag"))
    return {
        "id": str(case["id"]),
        "query": str(case["query"]),
        "category": str(case.get("category", "")),
        "language": str(case.get("language", "")),
        "should_use_rag": bool(case.get("should_use_rag")),
        "route_use_rag": bool(decision.use_rag),
        "route": str(decision.route),
        "route_ok": route_ok,
        "rewritten_query": str(decision.query),
        "hit_count": len(hits),
        "has_labels": has_labels,
        "precision_at_1": precision_at(relevance, 1),
        "precision_at_3": precision_at(relevance, 3),
        "precision_at_5": precision_at(relevance, 5),
        "recall_at_5": recall_at(relevance, relevant_count=max(1, len(relevant_ids)) if has_labels else max(1, sum(relevance)), k=5),
        "recall_at_10": recall_at(relevance, relevant_count=max(1, len(relevant_ids)) if has_labels else max(1, sum(relevance)), k=10),
        "mrr": reciprocal_rank(relevance),
        "ndcg_at_5": ndcg_at(relevance, 5),
        "ndcg_at_10": ndcg_at(relevance, 10),
        "first_relevant_rank": first_relevant_rank(relevance),
        "hit_rate": 1.0 if any(relevance) else 0.0,
        "source_hint_hit": bool(expected_source_hints and any(hint in source_blob for hint in expected_source_hints)),
        "top_score": float(hits[0].score) if hits else 0.0,
        "top_source": str(hits[0].source_relpath) if hits else "",
        "irrelevant_labeled_hits": sum(1 for hit_id in hit_ids if hit_id in irrelevant_ids),
        "latency_ms": {"route": route_ms, "search": search_ms, "total": route_ms + search_ms},
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if row["should_use_rag"]]
    tp = sum(1 for row in rows if row["should_use_rag"] and row["route_use_rag"])
    fp = sum(1 for row in rows if not row["should_use_rag"] and row["route_use_rag"])
    fn = sum(1 for row in rows if row["should_use_rag"] and not row["route_use_rag"])
    total_latencies = [row["latency_ms"]["total"] for row in rows]
    return {
        "cases": len(rows),
        "labeled_cases": sum(1 for row in rows if row["has_labels"]),
        "route_accuracy": mean([1.0 if row["route_ok"] else 0.0 for row in rows]),
        "route_precision": tp / (tp + fp) if tp + fp else 0.0,
        "route_recall": tp / (tp + fn) if tp + fn else 0.0,
        "precision_at_1": mean([row["precision_at_1"] for row in positives]),
        "precision_at_3": mean([row["precision_at_3"] for row in positives]),
        "precision_at_5": mean([row["precision_at_5"] for row in positives]),
        "recall_at_5": mean([row["recall_at_5"] for row in positives]),
        "recall_at_10": mean([row["recall_at_10"] for row in positives]),
        "mrr": mean([row["mrr"] for row in positives]),
        "ndcg_at_5": mean([row["ndcg_at_5"] for row in positives]),
        "ndcg_at_10": mean([row["ndcg_at_10"] for row in positives]),
        "hit_rate": mean([row["hit_rate"] for row in positives]),
        "latency_p50_ms": percentile(total_latencies, 50),
        "latency_p95_ms": percentile(total_latencies, 95),
        "latency_p99_ms": percentile(total_latencies, 99),
    }


def precision_at(relevance: list[int], k: int) -> float:
    if k <= 0:
        return 0.0
    return sum(relevance[:k]) / k


def recall_at(relevance: list[int], *, relevant_count: int, k: int) -> float:
    if relevant_count <= 0:
        return 0.0
    return min(1.0, sum(relevance[:k]) / relevant_count)


def reciprocal_rank(relevance: list[int]) -> float:
    rank = first_relevant_rank(relevance)
    return 1.0 / rank if rank else 0.0


def first_relevant_rank(relevance: list[int]) -> int:
    for index, value in enumerate(relevance, start=1):
        if value:
            return index
    return 0


def ndcg_at(relevance: list[int], k: int) -> float:
    gains = relevance[:k]
    dcg = sum(value / math.log2(index + 2) for index, value in enumerate(gains))
    ideal = sorted(relevance, reverse=True)[:k]
    idcg = sum(value / math.log2(index + 2) for index, value in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def mean(values) -> float:
    values = list(values)
    return float(sum(values) / len(values)) if values else 0.0


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=100, method="inclusive")[max(0, min(99, p - 1))])


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys() if key != "latency_ms"}) + [
        "latency_route_ms",
        "latency_search_ms",
        "latency_total_ms",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            flat = {key: value for key, value in row.items() if key != "latency_ms"}
            flat["latency_route_ms"] = row["latency_ms"]["route"]
            flat["latency_search_ms"] = row["latency_ms"]["search"]
            flat["latency_total_ms"] = row["latency_ms"]["total"]
            writer.writerow(flat)


if __name__ == "__main__":
    raise SystemExit(main())
