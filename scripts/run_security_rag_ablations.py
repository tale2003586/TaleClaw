from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import build_security_route_classifier_from_env, build_security_retrieval_router_from_env
from scripts.eval_security_rag_v2 import evaluate_case, load_cases, summarize, write_csv


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / ".evals" / "security_rag_ablations"


@dataclass(frozen=True)
class AblationConfig:
    name: str
    description: str
    collection: str
    env: dict[str, str]
    use_reranker: bool = False


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run Security RAG ablation experiments.")
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--search-all", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--use-llm-classifier", action="store_true")
    parser.add_argument("--only", action="append", default=[], help="Run only named config; may repeat.")
    args = parser.parse_args()

    cases = load_cases(Path(args.testset))
    run_id = "ablation_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    configs = default_configs()
    if args.only:
        selected = set(args.only)
        configs = [item for item in configs if item.name in selected]
        unknown = selected - {item.name for item in configs}
        if unknown:
            raise SystemExit(f"Unknown ablation config(s): {', '.join(sorted(unknown))}")

    reports: list[dict[str, Any]] = []
    for config in configs:
        print(f"[ablation] START {config.name}", flush=True)
        started = time.perf_counter()
        report = run_config(
            config,
            cases=cases,
            top_k=args.top_k,
            min_score=args.min_score,
            search_all=args.search_all,
            use_llm_classifier=args.use_llm_classifier,
        )
        report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        reports.append(report)
        write_report_files(output_dir, report)
        status = report["status"]
        summary = report.get("summary") or {}
        print(
            f"[ablation] {status.upper()}  {config.name} "
            f"p@1={summary.get('precision_at_1', 0):.4f} "
            f"mrr={summary.get('mrr', 0):.4f} "
            f"hit={summary.get('hit_rate', 0):.4f}",
            flush=True,
        )

    payload = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "testset": str(Path(args.testset)),
        "case_count": len(cases),
        "search_all": args.search_all,
        "use_llm_classifier": args.use_llm_classifier,
        "top_k": args.top_k,
        "reports": reports,
        "deltas": compute_deltas(reports),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (output_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"[ablation] DONE artifacts={output_dir}", flush=True)
    return 0


def default_configs() -> list[AblationConfig]:
    bge_model = os.getenv("SECURITY_RAG_EMBEDDING_MODEL", str(ROOT / "models" / "bge-m3"))
    bge_small_model = _local_model_or_repo(
        ROOT / "models" / "bge-small-zh-v1.5",
        "BAAI/bge-small-zh-v1.5",
    )
    reranker_model = _local_model_or_repo(
        ROOT / "models" / "bge-reranker-v2-m3",
        os.getenv("SECURITY_RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
    )
    return [
        AblationConfig(
            name="legacy_simple_dense",
            description=(
                "Legacy raw/length-style chunk collection. This is the closest "
                "available old baseline, but it uses the old 512-d embedding space."
            ),
            collection="code_security_kb",
            env={
                "SECURITY_RAG_EMBEDDING_PROVIDER": "fastembed",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_small_model,
                "SECURITY_RAG_VECTOR_SIZE": "512",
                "SECURITY_RAG_HYBRID_ENABLED": "0",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "",
                "SECURITY_RAG_RERANKER_ENABLED": "0",
                "SECURITY_RAG_CACHE_ENABLED": "0",
            },
        ),
        AblationConfig(
            name="precise_dense",
            description="Current structured chunking, dense BGE-M3 retrieval, no hybrid, no reranker.",
            collection="code_security_kb_bge_m3",
            env={
                "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_model,
                "SECURITY_RAG_VECTOR_SIZE": "1024",
                "SECURITY_RAG_HYBRID_ENABLED": "0",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "",
                "SECURITY_RAG_RERANKER_ENABLED": "0",
                "SECURITY_RAG_CACHE_ENABLED": "0",
            },
        ),
        AblationConfig(
            name="precise_hybrid",
            description="Current structured chunking with BGE-M3 dense + hash sparse Qdrant RRF hybrid.",
            collection="code_security_kb_bge_m3_hybrid",
            env={
                "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_model,
                "SECURITY_RAG_VECTOR_SIZE": "1024",
                "SECURITY_RAG_HYBRID_ENABLED": "1",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "hash",
                "SECURITY_RAG_SPARSE_HASH_SIZE": os.getenv("SECURITY_RAG_SPARSE_HASH_SIZE", "1048576"),
                "SECURITY_RAG_RERANKER_ENABLED": "0",
                "SECURITY_RAG_CACHE_ENABLED": "0",
            },
        ),
        AblationConfig(
            name="precise_hybrid_reranker",
            description="Current structured + hybrid retrieval, followed by configured FlagEmbedding reranker.",
            collection="code_security_kb_bge_m3_hybrid",
            env={
                "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_model,
                "SECURITY_RAG_VECTOR_SIZE": "1024",
                "SECURITY_RAG_HYBRID_ENABLED": "1",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "hash",
                "SECURITY_RAG_SPARSE_HASH_SIZE": os.getenv("SECURITY_RAG_SPARSE_HASH_SIZE", "1048576"),
                "SECURITY_RAG_RERANKER_ENABLED": "1",
                "SECURITY_RAG_RERANKER_MODEL": reranker_model,
                "SECURITY_RAG_CACHE_ENABLED": "0",
            },
            use_reranker=True,
        ),
    ]


def _local_model_or_repo(path: Path, fallback: str) -> str:
    return str(path) if path.exists() and any(path.iterdir()) else fallback


def run_config(
    config: AblationConfig,
    *,
    cases: list[dict[str, Any]],
    top_k: int,
    min_score: float | None,
    search_all: bool,
    use_llm_classifier: bool,
) -> dict[str, Any]:
    with env_overlay(config.env):
        try:
            router = build_security_retrieval_router_from_env()
            classifier = build_security_route_classifier_from_env(
                config=router.config,
                enabled=use_llm_classifier,
            )
            index = build_security_index_from_env(collection=config.collection)
            rows = []
            for case in cases:
                started = time.perf_counter()
                decision = router.route(str(case["query"]), llm_classifier=classifier)
                route_ms = (time.perf_counter() - started) * 1000
                hits = []
                search_ms = 0.0
                if decision.use_rag or search_all:
                    search_started = time.perf_counter()
                    hits = index.search(
                        decision.query,
                        top_k=top_k,
                        min_score=min_score if min_score is not None else decision.min_score,
                        use_reranker=config.use_reranker,
                        use_cache=False,
                    )
                    search_ms = (time.perf_counter() - search_started) * 1000
                rows.append(evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=search_ms))
            return {
                "name": config.name,
                "description": config.description,
                "status": "ok",
                "collection": config.collection,
                "env": config.env,
                "summary": summarize(rows),
                "cases": rows,
            }
        except Exception as exc:
            return {
                "name": config.name,
                "description": config.description,
                "status": "error",
                "collection": config.collection,
                "env": config.env,
                "error": f"{type(exc).__name__}: {exc}",
                "summary": {},
                "cases": [],
            }


def write_report_files(output_dir: Path, report: dict[str, Any]) -> None:
    name = report["name"]
    path = output_dir / f"{name}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("cases"):
        write_csv(output_dir / f"{name}.csv", report["cases"])


def compute_deltas(reports: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_name = {item["name"]: item for item in reports if item.get("status") == "ok"}
    comparisons = {
        "chunking_precise_minus_legacy": ("precise_dense", "legacy_simple_dense"),
        "hybrid_minus_dense": ("precise_hybrid", "precise_dense"),
        "reranker_minus_hybrid": ("precise_hybrid_reranker", "precise_hybrid"),
    }
    metrics = [
        "route_accuracy",
        "precision_at_1",
        "precision_at_3",
        "precision_at_5",
        "recall_at_5",
        "recall_at_10",
        "mrr",
        "ndcg_at_5",
        "ndcg_at_10",
        "hit_rate",
        "latency_p50_ms",
        "latency_p95_ms",
    ]
    deltas: dict[str, dict[str, float]] = {}
    for label, (after, before) in comparisons.items():
        if after not in by_name or before not in by_name:
            continue
        after_summary = by_name[after].get("summary") or {}
        before_summary = by_name[before].get("summary") or {}
        deltas[label] = {
            metric: float(after_summary.get(metric, 0.0)) - float(before_summary.get(metric, 0.0))
            for metric in metrics
        }
    return deltas


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Security RAG Ablation Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Testset: `{payload['testset']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Search all: `{payload['search_all']}`",
        f"- LLM classifier: `{payload['use_llm_classifier']}`",
        f"- Top K: `{payload['top_k']}`",
        "",
        "## Summary",
        "",
        "| config | status | route acc | p@1 | p@3 | p@5 | r@10 | mrr | ndcg@10 | hit | p50 ms | p95 ms |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in payload["reports"]:
        summary = report.get("summary") or {}
        lines.append(
            "| {name} | {status} | {route_accuracy} | {p1} | {p3} | {p5} | {r10} | {mrr} | {ndcg10} | {hit} | {p50} | {p95} |".format(
                name=report["name"],
                status=report["status"],
                route_accuracy=_fmt(summary.get("route_accuracy")),
                p1=_fmt(summary.get("precision_at_1")),
                p3=_fmt(summary.get("precision_at_3")),
                p5=_fmt(summary.get("precision_at_5")),
                r10=_fmt(summary.get("recall_at_10")),
                mrr=_fmt(summary.get("mrr")),
                ndcg10=_fmt(summary.get("ndcg_at_10")),
                hit=_fmt(summary.get("hit_rate")),
                p50=_fmt(summary.get("latency_p50_ms")),
                p95=_fmt(summary.get("latency_p95_ms")),
            )
        )
    lines.extend(["", "## Deltas", ""])
    if payload["deltas"]:
        lines.append("| comparison | p@1 | p@5 | r@10 | mrr | ndcg@10 | hit | p50 ms |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, delta in payload["deltas"].items():
            lines.append(
                "| {name} | {p1} | {p5} | {r10} | {mrr} | {ndcg10} | {hit} | {p50} |".format(
                    name=name,
                    p1=_fmt(delta.get("precision_at_1"), signed=True),
                    p5=_fmt(delta.get("precision_at_5"), signed=True),
                    r10=_fmt(delta.get("recall_at_10"), signed=True),
                    mrr=_fmt(delta.get("mrr"), signed=True),
                    ndcg10=_fmt(delta.get("ndcg_at_10"), signed=True),
                    hit=_fmt(delta.get("hit_rate"), signed=True),
                    p50=_fmt(delta.get("latency_p50_ms"), signed=True),
                )
            )
    else:
        lines.append("No comparable successful configs.")
    errors = [report for report in payload["reports"] if report.get("status") != "ok"]
    if errors:
        lines.extend(["", "## Errors", ""])
        for report in errors:
            lines.append(f"- `{report['name']}`: {report.get('error', 'unknown error')}")
    lines.extend([
        "",
        "## Notes",
        "",
        "- `legacy_simple_dense` is the available old raw/length-style collection. Its chunking and embedding space both differ from the current BGE-M3 collections, so treat it as a historical reference line rather than a perfectly isolated chunking-only variable.",
        "- The retrieval metrics use existing expected-term fallback labels when `relevant_chunk_ids` are absent.",
        "",
    ])
    return "\n".join(lines)


def _fmt(value: Any, *, signed: bool = False) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:+.4f}" if signed else f"{number:.4f}"


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
