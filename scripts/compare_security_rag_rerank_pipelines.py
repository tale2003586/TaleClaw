from __future__ import annotations

import argparse
import contextlib
import csv
import json
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.env_loader import load_dotenv_file
from knowledge.security_rag import build_security_index_from_env
from retrieval import RewriteResult, build_security_route_classifier_from_env, build_security_retrieval_router_from_env
from scripts.eval_security_rag_v2 import evaluate_case, load_cases, summarize, write_csv


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_user_questions_testset.jsonl"
FALLBACK_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / ".evals" / "security_rag_rerank_compare"
METRICS = [
    "route_accuracy",
    "route_precision",
    "route_recall",
    "precision_at_1",
    "precision_at_3",
    "precision_at_5",
    "recall_at_10",
    "mrr",
    "ndcg_at_10",
    "hit_rate",
    "latency_p50_ms",
    "latency_p95_ms",
]


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(
        description=(
            "Compare original router.route()+search+rerank with the current "
            "tiered route_with_retrieval pipeline."
        )
    )
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET if DEFAULT_TESTSET.exists() else FALLBACK_TESTSET))
    parser.add_argument("--collection", default="code_security_kb_bge_m3")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--min-score", type=float, default=None)
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--sample-seed", type=int, default=20260625)
    parser.add_argument("--progress-every", type=int, default=25)
    parser.add_argument(
        "--comparison",
        choices=["rerank-pipelines", "tiered-rewrite"],
        default="rerank-pipelines",
        help=(
            "rerank-pipelines compares old route()+search+rerank with tiered; "
            "tiered-rewrite compares tiered rewrite off vs on."
        ),
    )
    parser.add_argument("--embedding-model", default=str(ROOT / "models" / "bge-m3"))
    parser.add_argument("--reranker-model", default=str(_local_model_or_repo(ROOT / "models" / "bge-reranker-v2-m3", "BAAI/bge-reranker-v2-m3")))
    parser.add_argument("--embedding-device", default="")
    parser.add_argument("--reranker-candidates", type=int, default=None)
    parser.add_argument(
        "--search-all",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="For the original route_search baseline, search even when router says no.",
    )
    parser.add_argument(
        "--hybrid-enabled",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Enable hybrid dense+sparse retrieval so the router can evaluate dense/hybrid agreement.",
    )
    parser.add_argument("--use-llm-classifier", action="store_true")
    args = parser.parse_args()

    cases = load_cases(Path(args.testset))
    if args.limit_cases is not None:
        limit = min(len(cases), max(1, int(args.limit_cases)))
        cases = random.Random(int(args.sample_seed)).sample(cases, limit)

    run_id = "rerank_compare_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    output_dir = Path(args.output_root) / run_id
    output_dir.mkdir(parents=True, exist_ok=True)

    env = {
        "SECURITY_RAG_EMBEDDING_PROVIDER": "bge_m3",
        "SECURITY_RAG_EMBEDDING_MODEL": str(args.embedding_model),
        "SECURITY_RAG_VECTOR_SIZE": "1024",
        "SECURITY_RAG_HYBRID_ENABLED": "1" if args.hybrid_enabled else "0",
        "SECURITY_RAG_SPARSE_EMBEDDING_PROVIDER": "",
        "SECURITY_RAG_CACHE_ENABLED": "0",
        "SECURITY_RAG_RERANKER_ENABLED": "1",
        "SECURITY_RAG_RERANKER_MODEL": str(args.reranker_model),
    }
    if args.embedding_device.strip():
        env["SECURITY_RAG_EMBEDDING_DEVICE"] = args.embedding_device.strip()
    if args.reranker_candidates is not None:
        env["SECURITY_RAG_RERANKER_CANDIDATES"] = str(max(1, int(args.reranker_candidates)))

    with env_overlay(env):
        index = build_security_index_from_env(collection=args.collection)

        reports = []
        if args.comparison == "tiered-rewrite":
            router_off = build_security_retrieval_router_from_env()
            router_off.rewrite_provider = NoRewriteProvider()
            router_off.topic_expansions = []
            classifier_off = build_security_route_classifier_from_env(
                config=router_off.config,
                enabled=args.use_llm_classifier,
            )
            report = run_tiered_forced_rerank(
                cases,
                router=router_off,
                classifier=classifier_off,
                index=index,
                top_k=args.top_k,
                min_score=args.min_score,
                progress_every=args.progress_every,
                name="tiered_rewrite_off",
                description="Current route_with_retrieval() with query rewrite/decomposition provider disabled.",
            )
            reports.append(report)
            print_report(report)

            router_on = build_security_retrieval_router_from_env()
            classifier_on = build_security_route_classifier_from_env(
                config=router_on.config,
                enabled=args.use_llm_classifier,
            )
            report = run_tiered_forced_rerank(
                cases,
                router=router_on,
                classifier=classifier_on,
                index=index,
                top_k=args.top_k,
                min_score=args.min_score,
                progress_every=args.progress_every,
                name="tiered_rewrite_on",
                description=(
                    "Current route_with_retrieval() with configured rewrite provider enabled "
                    f"({type(router_on.rewrite_provider).__name__})."
                ),
            )
            reports.append(report)
            print_report(report)
        else:
            router = build_security_retrieval_router_from_env()
            classifier = build_security_route_classifier_from_env(
                config=router.config,
                enabled=args.use_llm_classifier,
            )
            report = run_route_search_rerank(
                cases,
                router=router,
                classifier=classifier,
                index=index,
                top_k=args.top_k,
                min_score=args.min_score,
                search_all=args.search_all,
                progress_every=args.progress_every,
            )
            reports.append(report)
            print_report(report)
            report = run_tiered_forced_rerank(
                cases,
                router=router,
                classifier=classifier,
                index=index,
                top_k=args.top_k,
                min_score=args.min_score,
                progress_every=args.progress_every,
            )
            reports.append(report)
            print_report(report)

    for report in reports:
        write_report_files(output_dir, report)

    payload = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "testset": str(Path(args.testset)),
        "collection": args.collection,
        "case_count": len(cases),
        "comparison": args.comparison,
        "top_k": args.top_k,
        "limit_cases": args.limit_cases,
        "sample_seed": args.sample_seed,
        "sampled_case_ids": [str(case.get("id") or "") for case in cases],
        "search_all": args.search_all,
        "use_llm_classifier": args.use_llm_classifier,
        "env": env,
        "reports": reports,
        "deltas": compute_deltas(reports),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_csv(output_dir / "summary.csv", reports)
    (output_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"[compare] DONE artifacts={output_dir}", flush=True)
    return 0


def run_route_search_rerank(
    cases: list[dict[str, Any]],
    *,
    router,
    classifier,
    index,
    top_k: int,
    min_score: float | None,
    search_all: bool,
    progress_every: int,
) -> dict[str, Any]:
    rows = []
    started = time.perf_counter()
    for index_number, case in enumerate(cases, start=1):
        query = str(case["query"])
        route_started = time.perf_counter()
        decision = router.route(query, llm_classifier=classifier)
        route_ms = (time.perf_counter() - route_started) * 1000
        hits = []
        search_ms = 0.0
        if decision.use_rag or search_all:
            search_started = time.perf_counter()
            hits = index.search(
                decision.query,
                top_k=top_k,
                min_score=min_score if min_score is not None else decision.min_score,
                use_reranker=True,
                use_cache=False,
            )
            search_ms = (time.perf_counter() - search_started) * 1000
        row = evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=search_ms)
        row.update(common_row_fields(case, query=query, decision=decision))
        row["pipeline"] = "route_search_rerank"
        row["route_action"] = "external_search_rerank" if hits else "no_search"
        row["search_count"] = 1 if hits else 0
        row["rerank_search_count"] = 1 if hits else 0
        row["search_stages"] = "external_search_rerank" if hits else ""
        row["search_tiers"] = ""
        rows.append(row)
        print_progress("original_route_search_rerank", index_number, len(cases), progress_every)
    return make_report(
        name="original_route_search_rerank",
        description="Original router.route() followed by one search with use_reranker=True.",
        rows=rows,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def run_tiered_forced_rerank(
    cases: list[dict[str, Any]],
    *,
    router,
    classifier,
    index,
    top_k: int,
    min_score: float | None,
    progress_every: int,
    name: str = "tiered_forced_rerank",
    description: str = "Current route_with_retrieval() with forced rerank after expansion/decomposition.",
) -> dict[str, Any]:
    rows = []
    started = time.perf_counter()
    for index_number, case in enumerate(cases, start=1):
        query = str(case["query"])
        route_started = time.perf_counter()
        plan = router.route_with_retrieval(
            query,
            index=index,
            llm_classifier=classifier,
            top_k=top_k,
            min_score=min_score,
            use_cache=False,
        )
        route_ms = (time.perf_counter() - route_started) * 1000
        decision = plan.decision
        hits = list(plan.hits)
        row = evaluate_case(case, decision, hits, route_ms=route_ms, search_ms=0.0)
        row.update(common_row_fields(case, query=query, decision=decision))
        row["pipeline"] = name
        row["route_action"] = str(plan.action)
        row["route_reason"] = str(plan.reason)
        row["search_count"] = len(plan.searches)
        row["rerank_search_count"] = sum(1 for record in plan.searches if record.use_reranker)
        row["search_stages"] = ",".join(record.stage for record in plan.searches)
        row["search_tiers"] = ",".join(record.tier for record in plan.searches)
        row["search_modes"] = ",".join(record.mode for record in plan.searches)
        row.update(latency_breakdown_fields(plan))
        rows.append(row)
        print_progress(name, index_number, len(cases), progress_every)
    return make_report(
        name=name,
        description=description,
        rows=rows,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def common_row_fields(case: dict[str, Any], *, query: str, decision) -> dict[str, Any]:
    return {
        "question_type": str(case.get("question_type") or case.get("category") or ""),
        "topic": str(case.get("topic") or ""),
        "searched_query": str(decision.query),
        "query_rewritten": bool(str(decision.query) != query),
        "route_reason": str(decision.reason),
    }


def make_report(*, name: str, description: str, rows: list[dict[str, Any]], duration_ms: float) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "summary": summarize(rows),
        "latency_breakdown": summarize_latency_breakdown(rows),
        "by_question_type": summarize_by(rows, "question_type"),
        "by_route": summarize_by(rows, "route"),
        "by_route_action": summarize_by(rows, "route_action"),
        "cases": rows,
        "duration_ms": round(duration_ms, 3),
    }


def summarize_by(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for key in sorted({str(row.get(field) or "") for row in rows}):
        subset = [row for row in rows if str(row.get(field) or "") == key]
        output[key or "unknown"] = summarize(subset)
    return output


def latency_breakdown_fields(plan) -> dict[str, Any]:
    search_embedding = 0.0
    search_qdrant = 0.0
    search_rerank = 0.0
    search_cache = 0.0
    search_total = 0.0
    for record in plan.searches:
        latency = record.latency_ms or {}
        search_embedding += sum(float(value) for key, value in latency.items() if str(key).startswith("embedding_"))
        search_qdrant += sum(float(value) for key, value in latency.items() if str(key).startswith("qdrant_"))
        search_rerank += float(latency.get("rerank", 0.0) or 0.0)
        search_cache += float(latency.get("cache", 0.0) or 0.0)
        search_total += float(latency.get("total", 0.0) or 0.0)

    rewrite_total = 0.0
    rewrite_llm = 0.0
    rewrite_provider = 0.0
    rewrite_cache_hits = 0
    rewrite_providers = []
    rewritten_queries = []
    for rewrite in plan.rewrites:
        latency = rewrite.latency_ms or {}
        rewrite_total += float(latency.get("total", latency.get("provider_total", 0.0)) or 0.0)
        rewrite_llm += float(latency.get("llm", 0.0) or 0.0)
        rewrite_provider += float(latency.get("provider_total", 0.0) or 0.0)
        if str((rewrite.metadata or {}).get("rewrite_cache_hit") or "").lower() == "true":
            rewrite_cache_hits += 1
        rewrite_providers.append(str(rewrite.provider))
        rewritten_queries.append(str(rewrite.query))

    return {
        "latency_search_embedding_ms": round(search_embedding, 3),
        "latency_search_qdrant_ms": round(search_qdrant, 3),
        "latency_search_reranker_ms": round(search_rerank, 3),
        "latency_search_cache_ms": round(search_cache, 3),
        "latency_search_internal_total_ms": round(search_total, 3),
        "latency_rewrite_total_ms": round(rewrite_total, 3),
        "latency_rewrite_llm_ms": round(rewrite_llm, 3),
        "latency_rewrite_provider_ms": round(rewrite_provider, 3),
        "rewrite_cache_hit_count": rewrite_cache_hits,
        "rewrite_providers": ",".join(rewrite_providers),
        "rewritten_queries": " | ".join(rewritten_queries),
        "search_latency_json": json.dumps(
            [
                {
                    "stage": record.stage,
                    "mode": record.mode,
                    "reranker": record.use_reranker,
                    "latency_ms": record.latency_ms,
                }
                for record in plan.searches
            ],
            ensure_ascii=False,
            sort_keys=True,
        ),
        "rewrite_latency_json": json.dumps(
            [
                {
                    "provider": rewrite.provider,
                    "query": rewrite.query,
                    "cache_hit": str((rewrite.metadata or {}).get("rewrite_cache_hit") or "false"),
                    "latency_ms": rewrite.latency_ms,
                }
                for rewrite in plan.rewrites
            ],
            ensure_ascii=False,
            sort_keys=True,
        ),
    }


def summarize_latency_breakdown(rows: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    fields = [
        "latency_rewrite_llm_ms",
        "latency_rewrite_total_ms",
        "latency_search_embedding_ms",
        "latency_search_qdrant_ms",
        "latency_search_reranker_ms",
        "latency_search_internal_total_ms",
    ]
    output: dict[str, dict[str, float]] = {}
    for field in fields:
        values = [float(row.get(field) or 0.0) for row in rows]
        output[field] = {
            "p50": round(_percentile(values, 50), 3),
            "p95": round(_percentile(values, 95), 3),
            "mean": round(sum(values) / len(values), 3) if values else 0.0,
        }
    return output


def _percentile(values: list[float], p: int) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    position = (len(values) - 1) * max(0, min(100, p)) / 100
    lower = int(position)
    upper = min(len(values) - 1, lower + 1)
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def print_progress(name: str, index_number: int, total: int, progress_every: int) -> None:
    if progress_every <= 0:
        return
    if index_number == total or index_number % progress_every == 0:
        print(f"[compare] {name} progress {index_number}/{total}", flush=True)


def print_report(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print(
        f"[compare] {report['name']} "
        f"route={summary.get('route_accuracy', 0):.4f} "
        f"p@1={summary.get('precision_at_1', 0):.4f} "
        f"mrr={summary.get('mrr', 0):.4f} "
        f"p50={summary.get('latency_p50_ms', 0):.0f}ms",
        flush=True,
    )


def compute_deltas(reports: list[dict[str, Any]]) -> dict[str, float]:
    by_name = {report["name"]: report for report in reports}
    if "tiered_rewrite_off" in by_name and "tiered_rewrite_on" in by_name:
        old = by_name["tiered_rewrite_off"]["summary"]
        new = by_name["tiered_rewrite_on"]["summary"]
    else:
        old = by_name["original_route_search_rerank"]["summary"]
        new = by_name["tiered_forced_rerank"]["summary"]
    return {metric: float(new.get(metric, 0.0)) - float(old.get(metric, 0.0)) for metric in METRICS}


def write_report_files(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / f"{report['name']}.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_csv(output_dir / f"{report['name']}.csv", report["cases"])


def write_summary_csv(path: Path, reports: list[dict[str, Any]]) -> None:
    fieldnames = ["name", "description", *METRICS, "duration_ms"]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        for report in reports:
            summary = report["summary"]
            writer.writerow({
                "name": report["name"],
                "description": report["description"],
                "duration_ms": report["duration_ms"],
                **{metric: summary.get(metric, "") for metric in METRICS},
            })


def render_markdown(payload: dict[str, Any]) -> str:
    comparison = payload.get("comparison") or "rerank-pipelines"
    if comparison == "tiered-rewrite":
        title = "Security RAG Tiered Rewrite Comparison"
        delta_label = "tiered_rewrite_on - tiered_rewrite_off"
        notes = [
            "- `tiered_rewrite_off` calls `router.route_with_retrieval()` with query rewrite/decomposition provider disabled.",
            "- `tiered_rewrite_on` calls `router.route_with_retrieval()` with the configured rewrite provider enabled.",
            "- Use `SECURITY_RAG_REWRITE_LLM_ENABLED=1` to make `tiered_rewrite_on` use the LLM rewrite provider; otherwise it uses the rule-based provider.",
            "- Per-case CSV includes `route_action`, `search_stages`, `search_tiers`, and `rerank_search_count`.",
        ]
    else:
        title = "Security RAG Rerank Pipeline Comparison"
        delta_label = "tiered_forced_rerank - original_route_search_rerank"
        notes = [
            "- `original_route_search_rerank` calls `router.route()` first, then one `index.search(..., use_reranker=True)`.",
            "- `tiered_forced_rerank` calls `router.route_with_retrieval()`; expansion and decomposition stages force `use_reranker=True` after this change.",
            "- Per-case CSV includes `route_action`, `search_stages`, `search_tiers`, and `rerank_search_count`.",
        ]
    lines = [
        f"# {title}",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Testset: `{payload['testset']}`",
        f"- Collection: `{payload['collection']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Comparison: `{comparison}`",
        f"- LLM classifier: `{payload['use_llm_classifier']}`",
        "",
        "## Summary",
        "",
        "| pipeline | p@1 | p@3 | p@5 | r@10 | mrr | hit | route acc | p50 ms | p95 ms |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    if comparison != "tiered-rewrite":
        lines.insert(7, f"- Search all for original baseline: `{payload['search_all']}`")
    for report in payload["reports"]:
        summary = report["summary"]
        lines.append(
            "| {name} | {p1} | {p3} | {p5} | {r10} | {mrr} | {hit} | {route} | {p50} | {p95} |".format(
                name=report["name"],
                p1=_fmt(summary.get("precision_at_1")),
                p3=_fmt(summary.get("precision_at_3")),
                p5=_fmt(summary.get("precision_at_5")),
                r10=_fmt(summary.get("recall_at_10")),
                mrr=_fmt(summary.get("mrr")),
                hit=_fmt(summary.get("hit_rate")),
                route=_fmt(summary.get("route_accuracy")),
                p50=_fmt(summary.get("latency_p50_ms")),
                p95=_fmt(summary.get("latency_p95_ms")),
            )
        )
    lines.extend(["", "## Latency Breakdown", ""])
    for report in payload["reports"]:
        breakdown = report.get("latency_breakdown") or {}
        if not breakdown:
            continue
        lines.extend([
            f"### `{report['name']}`",
            "",
            "| component | p50 ms | p95 ms | mean ms |",
            "| --- | ---: | ---: | ---: |",
        ])
        for component, stats in breakdown.items():
            lines.append(
                "| {component} | {p50} | {p95} | {mean} |".format(
                    component=component.replace("latency_", "").replace("_ms", ""),
                    p50=_fmt(stats.get("p50")),
                    p95=_fmt(stats.get("p95")),
                    mean=_fmt(stats.get("mean")),
                )
            )
        lines.append("")
    delta = payload["deltas"]
    lines.extend([
        "",
        "## Delta",
        "",
        f"`{delta_label}`",
        "",
        "| p@1 | p@3 | p@5 | r@10 | mrr | hit | route acc | p50 ms | p95 ms |",
        "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        "| {p1} | {p3} | {p5} | {r10} | {mrr} | {hit} | {route} | {p50} | {p95} |".format(
            p1=_fmt(delta.get("precision_at_1"), signed=True),
            p3=_fmt(delta.get("precision_at_3"), signed=True),
            p5=_fmt(delta.get("precision_at_5"), signed=True),
            r10=_fmt(delta.get("recall_at_10"), signed=True),
            mrr=_fmt(delta.get("mrr"), signed=True),
            hit=_fmt(delta.get("hit_rate"), signed=True),
            route=_fmt(delta.get("route_accuracy"), signed=True),
            p50=_fmt(delta.get("latency_p50_ms"), signed=True),
            p95=_fmt(delta.get("latency_p95_ms"), signed=True),
        ),
        "",
        "## Notes",
        "",
        *notes,
        "",
    ])
    return "\n".join(lines)


class NoRewriteProvider:
    def rewrite(self, request) -> RewriteResult:
        return RewriteResult(
            query=request.query,
            queries=[request.query],
            reason="Rewrite disabled for tiered rewrite ablation.",
            provider="off",
        )


def _local_model_or_repo(path: Path, fallback: str) -> str:
    return str(path) if path.exists() and any(path.iterdir()) else fallback


def _fmt(value: Any, *, signed: bool = False) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
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
