from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import random
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env
from scripts.compare_security_rag_rerank_pipelines import (
    METRICS,
    run_tiered_forced_rerank,
    write_report_files,
)
from scripts.eval_security_rag_v2 import load_cases


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_user_questions_testset.jsonl"
FALLBACK_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / ".evals" / "security_rag_full_power"


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Run a full-power Security RAG eval: LLM rewrite, pre-dense rewrite, "
            "hybrid retrieval, LLM route classifier, and rerank on every search stage."
        )
    )
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET if DEFAULT_TESTSET.exists() else FALLBACK_TESTSET))
    parser.add_argument("--collection", default="code_security_kb_bge_m3_hybrid")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--sample", action="store_true", help="Sample --limit-cases instead of taking the first N cases.")
    parser.add_argument("--sample-seed", type=int, default=20260628)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument("--embedding-model", default=str(_local_model_or_repo(ROOT / "models" / "bge-m3", "BAAI/bge-m3")))
    parser.add_argument(
        "--reranker-model",
        default=str(_local_model_or_repo(ROOT / "models" / "bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3")),
    )
    parser.add_argument("--embedding-device", default="")
    parser.add_argument("--sparse-provider", default="hash")
    parser.add_argument("--reranker-candidates", type=int, default=64)
    parser.add_argument("--rewrite-max-queries", type=int, default=4)
    parser.add_argument("--rewrite-max-tokens", type=int, default=512)
    parser.add_argument("--route-llm-max-tokens", type=int, default=350)
    parser.add_argument("--decompose-workers", type=int, default=4)
    parser.add_argument(
        "--pre-dense-provider",
        choices=["llm", "rule"],
        default="llm",
        help="Provider used by the always-on pre-dense rewrite stage.",
    )
    parser.add_argument(
        "--respect-router-rerank-gates",
        action="store_true",
        help="Do not force every router search to use rerank; only use the router's built-in rerank stages.",
    )
    parser.add_argument(
        "--no-llm-classifier",
        action="store_true",
        help="Keep rewrite on, but disable the route LLM classifier.",
    )
    args = parser.parse_args()

    cases = load_cases(Path(args.testset))
    if args.limit_cases is not None:
        limit = min(len(cases), max(1, int(args.limit_cases)))
        if args.sample:
            cases = random.Random(int(args.sample_seed)).sample(cases, limit)
        else:
            cases = cases[:limit]

    env = full_power_env(args)
    run_id = "full_power_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    print("[full-power] env overrides", flush=True)
    for key in sorted(env):
        print(f"[full-power]   {key}={env[key]}", flush=True)

    started = time.perf_counter()
    with env_overlay(env):
        router = build_security_retrieval_router_from_env()
        if not args.respect_router_rerank_gates:
            force_rerank_every_search(router)
        classifier = build_security_route_classifier_from_env(
            config=router.config,
            enabled=not args.no_llm_classifier,
        )
        index = build_security_index_from_env(collection=args.collection)
        serialize_reranker(index)
        report = run_tiered_forced_rerank(
            cases,
            router=router,
            classifier=classifier,
            index=index,
            top_k=args.top_k,
            min_score=args.min_score,
            progress_every=args.progress_every,
            name="full_power_rewrite_rerank",
            description=(
                "route_with_retrieval() with pre-dense LLM rewrite, LLM rewrite provider, "
                "hybrid retrieval, and rerank forced for every search stage."
            ),
        )

    payload = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "testset": str(Path(args.testset)),
        "collection": args.collection,
        "case_count": len(cases),
        "top_k": args.top_k,
        "limit_cases": args.limit_cases,
        "sample": bool(args.sample),
        "sample_seed": args.sample_seed,
        "sampled_case_ids": [str(case.get("id") or "") for case in cases],
        "llm_classifier": not args.no_llm_classifier,
        "force_rerank_every_search": not args.respect_router_rerank_gates,
        "serialize_reranker": True,
        "env": env,
        "report": report,
    }
    write_report_files(output_dir, report)
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_csv(output_dir / "summary.csv", report)
    (output_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"[full-power] DONE artifacts={output_dir}", flush=True)
    return 0


def full_power_env(args: argparse.Namespace) -> dict[str, str]:
    env = {
        "RAG_ENABLED": "1",
        "SECURITY_RAG_AB_ENABLED": "0",
        "SECURITY_RAG_CACHE_ENABLED": "0",
        "SECURITY_RAG_TRACE_ENABLED": "1",
        "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
        "SECURITY_RAG_EMBEDDING_MODEL": str(args.embedding_model),
        "SECURITY_RAG_VECTOR_SIZE": "1024",
        "SECURITY_RAG_HYBRID_ENABLED": "1",
        "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": str(args.sparse_provider),
        "SECURITY_RAG_RERANKER_ENABLED": "1",
        "SECURITY_RAG_RERANKER_MODEL": str(args.reranker_model),
        "SECURITY_RAG_RERANKER_CANDIDATES": str(max(1, int(args.reranker_candidates))),
        "SECURITY_RAG_ROUTE_LLM_ENABLED": "1",
        "SECURITY_RAG_ROUTE_LLM_PURPOSE": os.getenv("SECURITY_RAG_ROUTE_LLM_PURPOSE", "summary") or "summary",
        "SECURITY_RAG_ROUTE_LLM_MAX_TOKENS": str(max(1, int(args.route_llm_max_tokens))),
        "SECURITY_RAG_ROUTE_PRE_DENSE_REWRITE_ENABLED": "1",
        "SECURITY_RAG_PRE_DENSE_REWRITE_PROVIDER": str(args.pre_dense_provider),
        "SECURITY_RAG_PRE_DENSE_PARALLEL_ENABLED": "1",
        "SECURITY_RAG_REWRITE_LLM_ENABLED": "1",
        "SECURITY_RAG_REWRITE_LLM_PURPOSE": "security_router",
        "SECURITY_RAG_REWRITE_LLM_MAX_TOKENS": str(max(1, int(args.rewrite_max_tokens))),
        "SECURITY_RAG_REWRITE_LLM_MAX_QUERIES": str(max(1, int(args.rewrite_max_queries))),
        "SECURITY_RAG_DECOMPOSE_PARALLEL_ENABLED": "1",
        "SECURITY_RAG_DECOMPOSE_PARALLEL_WORKERS": str(max(1, int(args.decompose_workers))),
    }
    if str(args.embedding_device).strip():
        env["SECURITY_RAG_EMBEDDING_DEVICE"] = str(args.embedding_device).strip()
    return env


def force_rerank_every_search(router: Any) -> None:
    original_search = router._search

    def forced_search(
        index,
        *,
        query: str,
        top_k: int,
        min_score: float,
        mode: str,
        use_reranker: bool,
        use_cache: bool,
        stage: str,
        records: list,
        trace_callback=None,
    ):
        return original_search(
            index,
            query=query,
            top_k=top_k,
            min_score=min_score,
            mode=mode,
            use_reranker=True,
            use_cache=use_cache,
            stage=stage,
            records=records,
            trace_callback=trace_callback,
        )

    router._search = forced_search


def serialize_reranker(index: Any) -> None:
    """FlagEmbedding rerankers share a tokenizer that is not thread-safe."""
    target = getattr(index, "_index", getattr(index, "index", index))
    rerank_hits = getattr(target, "_rerank_hits", None)
    if rerank_hits is None:
        return
    lock = threading.Lock()

    def locked_rerank_hits(query: str, hits: list):
        with lock:
            return rerank_hits(query, hits)

    target._rerank_hits = locked_rerank_hits


def write_summary_csv(path: Path, report: dict[str, Any]) -> None:
    summary = report.get("summary") or {}
    fieldnames = ["name", "description", *METRICS, "duration_ms"]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "name": report.get("name", ""),
            "description": report.get("description", ""),
            "duration_ms": report.get("duration_ms", ""),
            **{metric: summary.get(metric, "") for metric in METRICS},
        })


def render_markdown(payload: dict[str, Any]) -> str:
    report = payload["report"]
    summary = report.get("summary") or {}
    latency = report.get("latency_breakdown") or {}
    lines = [
        "# Security RAG Full-Power Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Testset: `{payload['testset']}`",
        f"- Collection: `{payload['collection']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Top K: `{payload['top_k']}`",
        f"- LLM classifier: `{payload['llm_classifier']}`",
        f"- Force rerank every search: `{payload['force_rerank_every_search']}`",
        f"- Serialize reranker: `{payload['serialize_reranker']}`",
        "",
        "## Summary",
        "",
        "| pipeline | p@1 | p@3 | p@5 | r@10 | mrr | hit | route acc | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        (
            f"| {report['name']} | {_fmt(summary.get('precision_at_1'))} | "
            f"{_fmt(summary.get('precision_at_3'))} | {_fmt(summary.get('precision_at_5'))} | "
            f"{_fmt(summary.get('recall_at_10'))} | {_fmt(summary.get('mrr'))} | "
            f"{_fmt(summary.get('hit_rate'))} | {_fmt(summary.get('route_accuracy'))} | "
            f"{_fmt(summary.get('latency_p50_ms'))} | {_fmt(summary.get('latency_p95_ms'))} |"
        ),
        "",
        "## Latency",
        "",
        "| field | p50 | p95 | mean |",
        "| --- | ---: | ---: | ---: |",
    ]
    for field, values in latency.items():
        lines.append(
            f"| {field} | {_fmt(values.get('p50'))} | {_fmt(values.get('p95'))} | {_fmt(values.get('mean'))} |"
        )
    lines.extend([
        "",
        "## Notes",
        "",
        "- `SECURITY_RAG_ROUTE_PRE_DENSE_REWRITE_ENABLED=1` makes every case run pre-dense rewrite before the first dense retrieval.",
        "- `SECURITY_RAG_REWRITE_LLM_ENABLED=1` makes rewrite/decomposition use the LLM provider with rule-based fallback.",
        "- The script patches this test router so every retrieval stage calls search with `use_reranker=True` unless `--respect-router-rerank-gates` is set.",
    ])
    return "\n".join(lines) + "\n"


def _fmt(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        return f"{float(value):.4f}"
    except (TypeError, ValueError):
        return str(value)


def _local_model_or_repo(path: Path, fallback: str) -> str:
    return str(path) if path.exists() and any(path.iterdir()) else fallback


@contextlib.contextmanager
def env_overlay(values: dict[str, str]) -> Iterator[None]:
    old = {key: os.environ.get(key) for key in values}
    try:
        for key, value in values.items():
            if value == "":
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


if __name__ == "__main__":
    raise SystemExit(main())
