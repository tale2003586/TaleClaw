from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import (
    RetrievalDecision,
    build_security_route_classifier_from_env,
    build_security_retrieval_router_from_env,
)
from scripts.eval_security_rag_v2 import evaluate_case, load_cases, summarize, write_csv


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / ".evals" / "security_rag_matrix"
METRICS = [
    "route_accuracy",
    "route_precision",
    "route_recall",
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


@dataclass(frozen=True)
class MatrixConfig:
    name: str
    description: str
    pipeline: str
    chunking: str
    retrieval_mode: str
    router_enabled: bool
    rewriter_enabled: bool
    reranker_enabled: bool
    collection: str
    env: dict[str, str]


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Run the full Security RAG comparison matrix.")
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--limit-cases", type=int, default=None, help="Run only the first N cases for smoke tests.")
    parser.add_argument(
        "--embedding-device",
        default="",
        help="Override SECURITY_RAG_EMBEDDING_DEVICE, e.g. cuda:0 or cpu.",
    )
    parser.add_argument(
        "--reranker-candidates",
        type=int,
        default=None,
        help="Override SECURITY_RAG_RERANKER_CANDIDATES for speed/quality tradeoffs.",
    )
    parser.add_argument(
        "--search-all",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Search all cases regardless of router decision. Keeps retrieval metrics comparable while route metrics still measure router quality.",
    )
    parser.add_argument(
        "--use-llm-classifier",
        action="store_true",
        help="Allow the router's middle embedding band to call the LLM classifier.",
    )
    parser.add_argument(
        "--preset",
        choices=["core", "full"],
        default="core",
        help="core runs the minimal useful comparisons; full runs all supported combinations.",
    )
    parser.add_argument("--only", action="append", default=[], help="Run only named config; may repeat.")
    args = parser.parse_args()

    cases = load_cases(Path(args.testset))
    if args.limit_cases is not None:
        cases = cases[: max(1, int(args.limit_cases))]
    run_id = "matrix_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    configs = matrix_configs(args.preset)
    env_overrides = {}
    if args.embedding_device.strip():
        env_overrides["SECURITY_RAG_EMBEDDING_DEVICE"] = args.embedding_device.strip()
    if args.reranker_candidates is not None:
        env_overrides["SECURITY_RAG_RERANKER_CANDIDATES"] = str(max(1, int(args.reranker_candidates)))
    if env_overrides:
        configs = [with_env_overrides(config, env_overrides) for config in configs]
    if args.only:
        selected = set(args.only)
        configs = [item for item in configs if item.name in selected]
        unknown = selected - {item.name for item in configs}
        if unknown:
            raise SystemExit(f"Unknown matrix config(s): {', '.join(sorted(unknown))}")

    reports: list[dict[str, Any]] = []
    for config in configs:
        print(f"[matrix] START {config.name}", flush=True)
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
        summary = report.get("summary") or {}
        print(
            f"[matrix] {report['status'].upper()}  {config.name} "
            f"route={summary.get('route_accuracy', 0):.4f} "
            f"p@1={summary.get('precision_at_1', 0):.4f} "
            f"mrr={summary.get('mrr', 0):.4f} "
            f"p50={summary.get('latency_p50_ms', 0):.0f}ms",
            flush=True,
        )

    payload = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "testset": str(Path(args.testset)),
        "case_count": len(cases),
        "preset": args.preset,
        "search_all": args.search_all,
        "use_llm_classifier": args.use_llm_classifier,
        "top_k": args.top_k,
        "limit_cases": args.limit_cases,
        "embedding_device": args.embedding_device,
        "reranker_candidates": args.reranker_candidates,
        "reports": reports,
        "deltas": compute_deltas(reports),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_csv(output_dir / "summary.csv", reports)
    (output_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"[matrix] DONE artifacts={output_dir}", flush=True)
    return 0


def matrix_configs(preset: str) -> list[MatrixConfig]:
    bge_model = os.getenv("SECURITY_RAG_EMBEDDING_MODEL", str(ROOT / "models" / "bge-m3"))
    bge_small_model = os.getenv("SECURITY_RAG_LEGACY_EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5")
    reranker_model = _local_model_or_repo(
        ROOT / "models" / "bge-reranker-v2-m3",
        os.getenv("SECURITY_RAG_RERANKER_MODEL", "BAAI/bge-reranker-v2-m3"),
    )

    profiles = {
        ("legacy", "dense"): {
            "collection": "code_security_kb",
            "env": {
                "SECURITY_RAG_EMBEDDING_PROVIDER": "fastembed",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_small_model,
                "SECURITY_RAG_VECTOR_SIZE": "512",
                "SECURITY_RAG_HYBRID_ENABLED": "0",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "",
            },
        },
        ("precise", "dense"): {
            "collection": "code_security_kb_bge_m3",
            "env": {
                "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_model,
                "SECURITY_RAG_VECTOR_SIZE": "1024",
                "SECURITY_RAG_HYBRID_ENABLED": "0",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "",
            },
        },
        ("precise", "hybrid"): {
            "collection": "code_security_kb_bge_m3_hybrid",
            "env": {
                "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
                "SECURITY_RAG_EMBEDDING_MODEL": bge_model,
                "SECURITY_RAG_VECTOR_SIZE": "1024",
                "SECURITY_RAG_HYBRID_ENABLED": "1",
                "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "hash",
                "SECURITY_RAG_SPARSE_HASH_SIZE": os.getenv("SECURITY_RAG_SPARSE_HASH_SIZE", "1048576"),
            },
        },
    }

    def cfg(
        *,
        chunking: str,
        retrieval_mode: str,
        router: bool,
        rewriter: bool,
        reranker: bool,
        pipeline: str = "route_search",
        label: str | None = None,
    ) -> MatrixConfig:
        profile = profiles[(chunking, retrieval_mode)]
        name = label or _config_name(
            chunking=chunking,
            retrieval_mode=retrieval_mode,
            router=router,
            rewriter=rewriter,
            reranker=reranker,
            pipeline=pipeline,
        )
        env = {
            **profile["env"],
            "SECURITY_RAG_CACHE_ENABLED": "0",
            "SECURITY_RAG_RERANKER_ENABLED": "1" if reranker else "0",
        }
        if reranker:
            env["SECURITY_RAG_RERANKER_MODEL"] = reranker_model
        return MatrixConfig(
            name=name,
            description=(
                f"pipeline={pipeline}, "
                f"chunking={chunking}, retrieval={retrieval_mode}, "
                f"router={'on' if router else 'off'}, "
                f"rewriter={'on' if rewriter else 'off'}, "
                f"reranker={'on' if reranker else 'off'}"
            ),
            pipeline=pipeline,
            chunking=chunking,
            retrieval_mode=retrieval_mode,
            router_enabled=router,
            rewriter_enabled=rewriter,
            reranker_enabled=reranker,
            collection=str(profile["collection"]),
            env=env,
        )

    if preset == "core":
        return [
            cfg(chunking="legacy", retrieval_mode="dense", router=False, rewriter=False, reranker=False),
            cfg(chunking="legacy", retrieval_mode="dense", router=True, rewriter=True, reranker=False),
            cfg(chunking="precise", retrieval_mode="dense", router=False, rewriter=False, reranker=False),
            cfg(chunking="precise", retrieval_mode="dense", router=True, rewriter=False, reranker=False),
            cfg(chunking="precise", retrieval_mode="dense", router=True, rewriter=True, reranker=False),
            cfg(
                chunking="precise",
                retrieval_mode="dense",
                router=True,
                rewriter=True,
                reranker=False,
                pipeline="tiered",
                label="precise_dense_tiered_router_reranker_off",
            ),
            cfg(chunking="precise", retrieval_mode="hybrid", router=True, rewriter=True, reranker=False),
            cfg(chunking="precise", retrieval_mode="hybrid", router=True, rewriter=True, reranker=True),
        ]

    configs: list[MatrixConfig] = []
    for chunking, retrieval_mode in [("legacy", "dense"), ("precise", "dense"), ("precise", "hybrid")]:
        for router in [False, True]:
            rewriter_values = [False, True] if router else [False]
            for rewriter in rewriter_values:
                for reranker in [False, True]:
                    configs.append(
                        cfg(
                            chunking=chunking,
                            retrieval_mode=retrieval_mode,
                            router=router,
                            rewriter=rewriter,
                            reranker=reranker,
                        )
                    )
    configs.extend([
        cfg(
            chunking="precise",
            retrieval_mode="dense",
            router=True,
            rewriter=True,
            reranker=False,
            pipeline="tiered",
            label="precise_dense_tiered_router_reranker_off",
        ),
        cfg(
            chunking="precise",
            retrieval_mode="dense",
            router=True,
            rewriter=True,
            reranker=True,
            pipeline="tiered",
            label="precise_dense_tiered_router_reranker_on",
        ),
        cfg(
            chunking="precise",
            retrieval_mode="hybrid",
            router=True,
            rewriter=True,
            reranker=False,
            pipeline="tiered",
            label="precise_hybrid_tiered_router_reranker_off",
        ),
        cfg(
            chunking="precise",
            retrieval_mode="hybrid",
            router=True,
            rewriter=True,
            reranker=True,
            pipeline="tiered",
            label="precise_hybrid_tiered_router_reranker_on",
        ),
    ])
    return configs


def with_env_overrides(config: MatrixConfig, overrides: dict[str, str]) -> MatrixConfig:
    return replace(config, env={**config.env, **overrides})


def run_config(
    config: MatrixConfig,
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
            if config.pipeline == "tiered":
                return run_tiered_config(
                    config,
                    cases=cases,
                    router=router,
                    classifier=classifier,
                    index=index,
                    top_k=top_k,
                    min_score=min_score,
                )

            rows = []
            for case in cases:
                query = str(case["query"])
                started = time.perf_counter()
                decision = route_case(
                    query,
                    router=router,
                    router_enabled=config.router_enabled,
                    classifier=classifier,
                    top_k=top_k,
                )
                if not config.rewriter_enabled:
                    decision = replace(decision, query=query)
                route_ms = (time.perf_counter() - started) * 1000

                hits = []
                search_ms = 0.0
                if decision.use_rag or search_all:
                    search_started = time.perf_counter()
                    hits = index.search(
                        decision.query,
                        top_k=top_k,
                        min_score=min_score if min_score is not None else decision.min_score,
                        use_reranker=config.reranker_enabled,
                        use_cache=False,
                    )
                    search_ms = (time.perf_counter() - search_started) * 1000

                row = evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=search_ms)
                row["question_type"] = str(case.get("question_type") or case.get("category") or "")
                row["topic"] = str(case.get("topic") or "")
                row["searched_query"] = str(decision.query)
                row["query_rewritten"] = bool(decision.query != query)
                row["pipeline"] = config.pipeline
                row["route_action"] = "external_search" if hits else "no_search"
                row["route_reason"] = str(decision.reason)
                row["search_count"] = 1 if hits else 0
                row["search_stages"] = "external_search" if hits else ""
                row["search_modes"] = config.retrieval_mode if hits else ""
                row["search_tiers"] = ""
                rows.append(row)
            summary = summarize(rows)
            return {
                "name": config.name,
                "description": config.description,
                "status": "ok",
                "dimensions": config_dimensions(config),
                "collection": config.collection,
                "env": config.env,
                "summary": summary,
                "by_question_type": summarize_by(rows, "question_type"),
                "by_route": summarize_by(rows, "route"),
                "by_route_action": summarize_by(rows, "route_action"),
                "cases": rows,
            }
        except Exception as exc:
            return {
                "name": config.name,
                "description": config.description,
                "status": "error",
                "dimensions": config_dimensions(config),
                "collection": config.collection,
                "env": config.env,
                "error": f"{type(exc).__name__}: {exc}",
                "summary": {},
                "cases": [],
            }


def run_tiered_config(
    config: MatrixConfig,
    *,
    cases: list[dict[str, Any]],
    router,
    classifier,
    index,
    top_k: int,
    min_score: float | None,
) -> dict[str, Any]:
    rows = []
    for case in cases:
        query = str(case["query"])
        started = time.perf_counter()
        plan = router.route_with_retrieval(
            query,
            index=index,
            llm_classifier=classifier,
            top_k=top_k,
            min_score=min_score,
            use_cache=False,
        )
        route_ms = (time.perf_counter() - started) * 1000
        decision = plan.decision
        hits = list(plan.hits)
        row = evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=0.0)
        row["question_type"] = str(case.get("question_type") or case.get("category") or "")
        row["topic"] = str(case.get("topic") or "")
        row["searched_query"] = str(decision.query)
        row["query_rewritten"] = bool(decision.query != query)
        row["pipeline"] = config.pipeline
        row["route_action"] = str(plan.action)
        row["route_reason"] = str(plan.reason)
        row["search_count"] = len(plan.searches)
        row["search_stages"] = ",".join(record.stage for record in plan.searches)
        row["search_modes"] = ",".join(record.mode for record in plan.searches)
        row["search_tiers"] = ",".join(record.tier for record in plan.searches)
        row["llm_decision_route"] = str(plan.llm_decision.route) if plan.llm_decision else ""
        rows.append(row)
    summary = summarize(rows)
    return {
        "name": config.name,
        "description": config.description,
        "status": "ok",
        "dimensions": config_dimensions(config),
        "collection": config.collection,
        "env": config.env,
        "summary": summary,
        "by_question_type": summarize_by(rows, "question_type"),
        "by_route": summarize_by(rows, "route"),
        "by_route_action": summarize_by(rows, "route_action"),
        "cases": rows,
    }


def route_case(
    query: str,
    *,
    router,
    router_enabled: bool,
    classifier,
    top_k: int,
) -> RetrievalDecision:
    if router_enabled:
        return router.route(query, llm_classifier=classifier)
    return RetrievalDecision(
        use_rag=True,
        target="security_kb",
        query=query,
        confidence=1.0,
        reason="Router disabled for ablation; always search.",
        route="router_off",
        top_k=max(1, int(top_k)),
        min_score=0.0,
    )


def summarize_by(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    keys = sorted({str(row.get(field) or "") for row in rows})
    for key in keys:
        subset = [row for row in rows if str(row.get(field) or "") == key]
        output[key or "unknown"] = summarize(subset)
    return output


def compute_deltas(reports: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    by_name = {item["name"]: item for item in reports if item.get("status") == "ok"}
    comparisons = {
        "new_chunking_minus_old_chunking": (
            "precise_dense_router_off_rewriter_off_reranker_off",
            "legacy_dense_router_off_rewriter_off_reranker_off",
        ),
        "router_on_minus_router_off": (
            "precise_dense_router_on_rewriter_off_reranker_off",
            "precise_dense_router_off_rewriter_off_reranker_off",
        ),
        "rewriter_on_minus_rewriter_off": (
            "precise_dense_router_on_rewriter_on_reranker_off",
            "precise_dense_router_on_rewriter_off_reranker_off",
        ),
        "hybrid_minus_dense": (
            "precise_hybrid_router_on_rewriter_on_reranker_off",
            "precise_dense_router_on_rewriter_on_reranker_off",
        ),
        "reranker_minus_hybrid": (
            "precise_hybrid_router_on_rewriter_on_reranker_on",
            "precise_hybrid_router_on_rewriter_on_reranker_off",
        ),
        "tiered_minus_route_search": (
            "precise_dense_tiered_router_reranker_off",
            "precise_dense_router_on_rewriter_on_reranker_off",
        ),
    }
    deltas: dict[str, dict[str, float]] = {}
    for label, (after, before) in comparisons.items():
        if after not in by_name or before not in by_name:
            continue
        after_summary = by_name[after].get("summary") or {}
        before_summary = by_name[before].get("summary") or {}
        deltas[label] = {
            metric: float(after_summary.get(metric, 0.0)) - float(before_summary.get(metric, 0.0))
            for metric in METRICS
        }
    return deltas


def write_report_files(output_dir: Path, report: dict[str, Any]) -> None:
    path = output_dir / f"{report['name']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("cases"):
        write_csv(output_dir / f"{report['name']}.csv", report["cases"])


def write_summary_csv(path: Path, reports: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "status",
        "pipeline",
        "chunking",
        "retrieval_mode",
        "router_enabled",
        "rewriter_enabled",
        "reranker_enabled",
        *METRICS,
        "duration_ms",
        "error",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for report in reports:
            dims = report.get("dimensions") or {}
            summary = report.get("summary") or {}
            row = {
                "name": report["name"],
                "status": report["status"],
                "pipeline": dims.get("pipeline", ""),
                "chunking": dims.get("chunking", ""),
                "retrieval_mode": dims.get("retrieval_mode", ""),
                "router_enabled": dims.get("router_enabled", ""),
                "rewriter_enabled": dims.get("rewriter_enabled", ""),
                "reranker_enabled": dims.get("reranker_enabled", ""),
                "duration_ms": report.get("duration_ms", ""),
                "error": report.get("error", ""),
            }
            row.update({metric: summary.get(metric, "") for metric in METRICS})
            writer.writerow(row)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Security RAG Matrix Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Testset: `{payload['testset']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Preset: `{payload['preset']}`",
        f"- Search all: `{payload['search_all']}`",
        f"- LLM classifier: `{payload['use_llm_classifier']}`",
        f"- Top K: `{payload['top_k']}`",
        f"- Limit cases: `{payload.get('limit_cases')}`",
        f"- Embedding device: `{payload.get('embedding_device') or 'env default'}`",
        f"- Reranker candidates: `{payload.get('reranker_candidates') or 'env default'}`",
        "",
        "## Summary",
        "",
        "| config | status | pipeline | chunk | retrieval | router | rewrite | rerank | route acc | p@1 | p@5 | r@10 | mrr | hit | p50 ms | p95 ms |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in payload["reports"]:
        dims = report.get("dimensions") or {}
        summary = report.get("summary") or {}
        lines.append(
            "| {name} | {status} | {pipeline} | {chunk} | {retrieval} | {router} | {rewrite} | {rerank} | {route} | {p1} | {p5} | {r10} | {mrr} | {hit} | {p50} | {p95} |".format(
                name=report["name"],
                status=report["status"],
                pipeline=dims.get("pipeline", ""),
                chunk=dims.get("chunking", ""),
                retrieval=dims.get("retrieval_mode", ""),
                router=_onoff(dims.get("router_enabled")),
                rewrite=_onoff(dims.get("rewriter_enabled")),
                rerank=_onoff(dims.get("reranker_enabled")),
                route=_fmt(summary.get("route_accuracy")),
                p1=_fmt(summary.get("precision_at_1")),
                p5=_fmt(summary.get("precision_at_5")),
                r10=_fmt(summary.get("recall_at_10")),
                mrr=_fmt(summary.get("mrr")),
                hit=_fmt(summary.get("hit_rate")),
                p50=_fmt(summary.get("latency_p50_ms")),
                p95=_fmt(summary.get("latency_p95_ms")),
            )
        )

    lines.extend(["", "## Key Deltas", ""])
    if payload["deltas"]:
        lines.append("| comparison | route acc | p@1 | p@5 | r@10 | mrr | hit | p50 ms |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, delta in payload["deltas"].items():
            lines.append(
                "| {name} | {route} | {p1} | {p5} | {r10} | {mrr} | {hit} | {p50} |".format(
                    name=name,
                    route=_fmt(delta.get("route_accuracy"), signed=True),
                    p1=_fmt(delta.get("precision_at_1"), signed=True),
                    p5=_fmt(delta.get("precision_at_5"), signed=True),
                    r10=_fmt(delta.get("recall_at_10"), signed=True),
                    mrr=_fmt(delta.get("mrr"), signed=True),
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
        "- `search_all=True` keeps retrieval metrics comparable even when the router says no; route metrics still measure router correctness.",
        "- `pipeline=route_search` uses `router.route()` followed by one external search; `pipeline=tiered` calls `router.route_with_retrieval()` and reports its end-to-end returned hits.",
        "- Tiered configs do not add an extra `search_all` fallback because the tiered router has already performed its own retrieval/escalation steps.",
        "- Use `--no-search-all` for an end-to-end gate where router false negatives also suppress retrieval.",
        "- `legacy` uses the old raw/simple collection and old embedding space; treat it as a historical baseline, not a perfectly isolated chunking-only variable.",
        "- Metrics use `relevant_chunk_ids` when present; otherwise they fall back to `expected_terms` matching.",
        "",
    ])
    return "\n".join(lines)


def config_dimensions(config: MatrixConfig) -> dict[str, Any]:
    return {
        "pipeline": config.pipeline,
        "chunking": config.chunking,
        "retrieval_mode": config.retrieval_mode,
        "router_enabled": config.router_enabled,
        "rewriter_enabled": config.rewriter_enabled,
        "reranker_enabled": config.reranker_enabled,
    }


def _config_name(
    *,
    chunking: str,
    retrieval_mode: str,
    router: bool,
    rewriter: bool,
    reranker: bool,
    pipeline: str = "route_search",
) -> str:
    if pipeline == "tiered":
        return f"{chunking}_{retrieval_mode}_tiered_router_reranker_{'on' if reranker else 'off'}"
    return (
        f"{chunking}_{retrieval_mode}_"
        f"router_{'on' if router else 'off'}_"
        f"rewriter_{'on' if rewriter else 'off'}_"
        f"reranker_{'on' if reranker else 'off'}"
    )


def _local_model_or_repo(path: Path, fallback: str) -> str:
    return str(path) if path.exists() and any(path.iterdir()) else fallback


def _fmt(value: Any, *, signed: bool = False) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:+.4f}" if signed else f"{number:.4f}"


def _onoff(value: Any) -> str:
    return "on" if bool(value) else "off"


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
