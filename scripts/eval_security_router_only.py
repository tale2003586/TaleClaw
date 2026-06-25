from __future__ import annotations

import argparse
import contextlib
import csv
import json
import math
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.security_rag import build_security_embedding_provider_from_env
from retrieval import build_security_route_classifier_from_env
from retrieval.security_router import (
    DEFAULT_SECURITY_KEYWORDS,
    RetrievalDecision,
    SecurityRetrievalRouter,
    SecurityRouteConfig,
)
from runtime.env_loader import load_dotenv_file
from scripts.eval_security_rag_v2 import load_cases


DEFAULT_TESTSET = ROOT / "benchmarks" / "security_rag_user_questions_testset.jsonl"
FALLBACK_TESTSET = ROOT / "benchmarks" / "security_rag_testset.jsonl"
DEFAULT_OUTPUT_ROOT = ROOT / ".evals" / "security_router_only"
METRICS = [
    "accuracy",
    "balanced_accuracy",
    "precision",
    "recall",
    "specificity",
    "f1",
    "false_positive_rate",
    "false_negative_rate",
    "latency_p50_ms",
    "latency_p95_ms",
]


@dataclass(frozen=True)
class RouterEvalConfig:
    name: str
    description: str
    mode: str
    high_threshold: float
    low_threshold: float
    llm_accept_threshold: float
    keywords_enabled: bool
    llm_enabled: bool
    env: dict[str, str]


def main() -> int:
    load_dotenv_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Evaluate only the Security RAG router, without retrieval.")
    parser.add_argument("--testset", default=str(DEFAULT_TESTSET))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--limit-cases", type=int, default=None)
    parser.add_argument("--preset", choices=["core", "grid"], default="core")
    parser.add_argument("--include-llm", action="store_true", help="Also run configs that call the LLM classifier.")
    parser.add_argument("--include-no-keywords", action="store_true", help="Also test router behavior without keyword rules.")
    parser.add_argument("--no-baseline", action="store_true", help="Do not include always_use_rag baseline.")
    parser.add_argument("--low-thresholds", default="0.35,0.45,0.55")
    parser.add_argument("--high-thresholds", default="0.65,0.72,0.80")
    parser.add_argument("--llm-accept-thresholds", default="0.50,0.60,0.70")
    parser.add_argument("--embedding-device", default="", help="Override SECURITY_RAG_EMBEDDING_DEVICE, e.g. cpu.")
    parser.add_argument("--only", action="append", default=[], help="Run only named config; may repeat.")
    args = parser.parse_args()

    testset = Path(args.testset)
    if args.testset == str(DEFAULT_TESTSET) and not testset.exists():
        testset = FALLBACK_TESTSET
    cases = load_cases(testset)
    if args.limit_cases is not None:
        cases = cases[: max(1, int(args.limit_cases))]

    env = {}
    if args.embedding_device.strip():
        env["SECURITY_RAG_EMBEDDING_DEVICE"] = args.embedding_device.strip()

    with env_overlay(env):
        embeddings = build_security_embedding_provider_from_env()
        configs = build_configs(
            preset=args.preset,
            include_baseline=not args.no_baseline,
            include_llm=args.include_llm,
            include_no_keywords=args.include_no_keywords,
            low_thresholds=parse_float_list(args.low_thresholds),
            high_thresholds=parse_float_list(args.high_thresholds),
            llm_accept_thresholds=parse_float_list(args.llm_accept_thresholds),
            env=env,
        )
        if args.only:
            selected = set(args.only)
            configs = [config for config in configs if config.name in selected]
            unknown = selected - {config.name for config in configs}
            if unknown:
                raise SystemExit(f"Unknown router config(s): {', '.join(sorted(unknown))}")

        run_id = "router_eval_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_dir = Path(args.output_root) / run_id
        output_dir.mkdir(parents=True, exist_ok=True)

        reports = []
        for config in configs:
            print(f"[router] START {config.name}", flush=True)
            started = time.perf_counter()
            report = run_config(config, cases=cases, embeddings=embeddings)
            report["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
            reports.append(report)
            write_report_files(output_dir, report)
            summary = report.get("summary") or {}
            print(
                f"[router] {report['status'].upper()}  {config.name} "
                f"acc={summary.get('accuracy', 0):.4f} "
                f"bal={summary.get('balanced_accuracy', 0):.4f} "
                f"prec={summary.get('precision', 0):.4f} "
                f"rec={summary.get('recall', 0):.4f} "
                f"spec={summary.get('specificity', 0):.4f}",
                flush=True,
            )

    payload = {
        "run_id": run_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "testset": str(testset),
        "case_count": len(cases),
        "preset": args.preset,
        "include_llm": args.include_llm,
        "include_no_keywords": args.include_no_keywords,
        "limit_cases": args.limit_cases,
        "embedding_device": args.embedding_device,
        "reports": reports,
        "deltas": compute_deltas(reports),
    }
    (output_dir / "summary.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_summary_csv(output_dir / "summary.csv", reports)
    (output_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")
    print(f"[router] DONE artifacts={output_dir}", flush=True)
    return 0


def build_configs(
    *,
    preset: str,
    include_baseline: bool,
    include_llm: bool,
    include_no_keywords: bool,
    low_thresholds: list[float],
    high_thresholds: list[float],
    llm_accept_thresholds: list[float],
    env: dict[str, str],
) -> list[RouterEvalConfig]:
    configs: list[RouterEvalConfig] = []
    default_low = _env_float("SECURITY_RAG_ROUTE_LOW_THRESHOLD", 0.45)
    default_high = _env_float("SECURITY_RAG_ROUTE_HIGH_THRESHOLD", 0.72)
    default_llm_accept = _env_float("SECURITY_RAG_ROUTE_LLM_ACCEPT_THRESHOLD", 0.60)

    if include_baseline:
        configs.append(
            RouterEvalConfig(
                name="always_use_rag",
                description="Router disabled baseline: every query is routed to RAG.",
                mode="always_on",
                high_threshold=default_high,
                low_threshold=default_low,
                llm_accept_threshold=default_llm_accept,
                keywords_enabled=False,
                llm_enabled=False,
                env=env,
            )
        )

    if preset == "core":
        configs.append(
            make_config(
                low=default_low,
                high=default_high,
                llm_accept=default_llm_accept,
                keywords=True,
                llm=False,
                name="router_embedding_only",
                env=env,
            )
        )
        if include_no_keywords:
            configs.append(
                make_config(
                    low=default_low,
                    high=default_high,
                    llm_accept=default_llm_accept,
                    keywords=False,
                    llm=False,
                    name="router_embedding_only_no_keywords",
                    env=env,
                )
            )
        if include_llm:
            configs.append(
                make_config(
                    low=default_low,
                    high=default_high,
                    llm_accept=default_llm_accept,
                    keywords=True,
                    llm=True,
                    name="router_embedding_llm",
                    env=env,
                )
            )
            if include_no_keywords:
                configs.append(
                    make_config(
                        low=default_low,
                        high=default_high,
                        llm_accept=default_llm_accept,
                        keywords=False,
                        llm=True,
                        name="router_embedding_llm_no_keywords",
                        env=env,
                    )
                )
        return configs

    for low in low_thresholds:
        for high in high_thresholds:
            if low >= high:
                continue
            configs.append(
                make_config(
                    low=low,
                    high=high,
                    llm_accept=default_llm_accept,
                    keywords=True,
                    llm=False,
                    name=f"router_embed_low{fmt_name(low)}_high{fmt_name(high)}",
                    env=env,
                )
            )
            if include_no_keywords:
                configs.append(
                    make_config(
                        low=low,
                        high=high,
                        llm_accept=default_llm_accept,
                        keywords=False,
                        llm=False,
                        name=f"router_embed_no_kw_low{fmt_name(low)}_high{fmt_name(high)}",
                        env=env,
                    )
                )
            if include_llm:
                for llm_accept in llm_accept_thresholds:
                    configs.append(
                        make_config(
                            low=low,
                            high=high,
                            llm_accept=llm_accept,
                            keywords=True,
                            llm=True,
                            name=(
                                f"router_llm_low{fmt_name(low)}_"
                                f"high{fmt_name(high)}_accept{fmt_name(llm_accept)}"
                            ),
                            env=env,
                        )
                    )
    return configs


def make_config(
    *,
    low: float,
    high: float,
    llm_accept: float,
    keywords: bool,
    llm: bool,
    name: str,
    env: dict[str, str],
) -> RouterEvalConfig:
    return RouterEvalConfig(
        name=name,
        description=(
            f"low={low:.2f}, high={high:.2f}, "
            f"keywords={'on' if keywords else 'off'}, "
            f"llm={'on' if llm else 'off'}, "
            f"llm_accept={llm_accept:.2f}"
        ),
        mode="router",
        high_threshold=float(high),
        low_threshold=float(low),
        llm_accept_threshold=float(llm_accept),
        keywords_enabled=keywords,
        llm_enabled=llm,
        env=env,
    )


def run_config(config: RouterEvalConfig, *, cases: list[dict[str, Any]], embeddings) -> dict[str, Any]:
    try:
        rows: list[dict[str, Any]] = []
        router = None
        classifier = None
        if config.mode == "router":
            route_config = SecurityRouteConfig(
                high_threshold=config.high_threshold,
                low_threshold=config.low_threshold,
                llm_accept_threshold=config.llm_accept_threshold,
                default_top_k=_env_int("SECURITY_RAG_ROUTE_TOP_K", 5),
                min_score=_env_float("SECURITY_RAG_ROUTE_MIN_SCORE", 0.0),
            )
            router = SecurityRetrievalRouter(
                embeddings=embeddings,
                config=route_config,
                keywords=DEFAULT_SECURITY_KEYWORDS if config.keywords_enabled else {},
            )
            classifier = build_security_route_classifier_from_env(
                config=route_config,
                enabled=config.llm_enabled,
            )

        for case in cases:
            query = str(case["query"])
            started = time.perf_counter()
            if config.mode == "always_on":
                decision = RetrievalDecision(
                    use_rag=True,
                    target="security_kb",
                    query=query,
                    confidence=1.0,
                    reason="Router disabled baseline; always route to RAG.",
                    route="always_on",
                )
            else:
                assert router is not None
                decision = router.route(query, llm_classifier=classifier)
            latency_ms = (time.perf_counter() - started) * 1000
            rows.append(evaluate_case(case, decision, latency_ms=latency_ms))

        return {
            "name": config.name,
            "description": config.description,
            "status": "ok",
            "dimensions": config_dimensions(config),
            "summary": summarize(rows),
            "by_question_type": summarize_by(rows, "question_type"),
            "by_difficulty": summarize_by(rows, "difficulty"),
            "by_topic": summarize_by(rows, "topic_label"),
            "by_route": summarize_by(rows, "route"),
            "false_positives": [row for row in rows if row["confusion"] == "fp"],
            "false_negatives": [row for row in rows if row["confusion"] == "fn"],
            "cases": rows,
        }
    except Exception as exc:
        return {
            "name": config.name,
            "description": config.description,
            "status": "error",
            "dimensions": config_dimensions(config),
            "error": f"{type(exc).__name__}: {exc}",
            "summary": {},
            "cases": [],
        }


def evaluate_case(case: dict[str, Any], decision: RetrievalDecision, *, latency_ms: float) -> dict[str, Any]:
    should_use = bool(case.get("should_use_rag"))
    predicted = bool(decision.use_rag)
    if should_use and predicted:
        confusion = "tp"
    elif not should_use and predicted:
        confusion = "fp"
    elif should_use and not predicted:
        confusion = "fn"
    else:
        confusion = "tn"
    query = str(case["query"])
    return {
        "id": str(case["id"]),
        "source_id": str(case.get("source_id") or ""),
        "query": query,
        "category": str(case.get("category") or ""),
        "question_type": str(case.get("question_type") or case.get("category") or ""),
        "rag_need": str(case.get("rag_need") or ""),
        "topic": str(case.get("topic") or ""),
        "topic_label": str(case.get("topic_label") or ""),
        "difficulty": str(case.get("difficulty") or ""),
        "user_role": str(case.get("user_role") or ""),
        "should_use_rag": should_use,
        "route_use_rag": predicted,
        "route_ok": should_use == predicted,
        "confusion": confusion,
        "route": str(decision.route),
        "confidence": float(decision.confidence),
        "embedding_score": float(decision.embedding_score),
        "matched_intent": str(decision.matched_intent),
        "keyword_matches": ";".join(decision.keyword_matches),
        "llm_required": bool(decision.llm_required),
        "rewritten_query": str(decision.query),
        "query_rewritten": bool(str(decision.query) != query),
        "reason": str(decision.reason),
        "latency_ms": float(latency_ms),
    }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    tp = sum(1 for row in rows if row["confusion"] == "tp")
    fp = sum(1 for row in rows if row["confusion"] == "fp")
    fn = sum(1 for row in rows if row["confusion"] == "fn")
    tn = sum(1 for row in rows if row["confusion"] == "tn")
    latencies = [float(row["latency_ms"]) for row in rows]
    recall = tp / (tp + fn) if tp + fn else 0.0
    specificity = tn / (tn + fp) if tn + fp else 0.0
    precision = tp / (tp + fp) if tp + fp else 0.0
    negative_precision = tn / (tn + fn) if tn + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    mcc_denominator = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    return {
        "cases": total,
        "positives": tp + fn,
        "negatives": tn + fp,
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "accuracy": (tp + tn) / total if total else 0.0,
        "balanced_accuracy": (recall + specificity) / 2 if total else 0.0,
        "precision": precision,
        "recall": recall,
        "specificity": specificity,
        "negative_precision": negative_precision,
        "f1": f1,
        "mcc": ((tp * tn) - (fp * fn)) / mcc_denominator if mcc_denominator else 0.0,
        "false_positive_rate": fp / (fp + tn) if fp + tn else 0.0,
        "false_negative_rate": fn / (fn + tp) if fn + tp else 0.0,
        "predicted_positive_rate": (tp + fp) / total if total else 0.0,
        "predicted_negative_rate": (tn + fn) / total if total else 0.0,
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_p99_ms": percentile(latencies, 99),
        "confidence_mean": mean([float(row["confidence"]) for row in rows]),
        "embedding_score_mean": mean([float(row["embedding_score"]) for row in rows]),
        "rewritten_rate": mean([1.0 if row["query_rewritten"] else 0.0 for row in rows]),
    }


def summarize_by(rows: list[dict[str, Any]], field: str) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    keys = sorted({str(row.get(field) or "") for row in rows})
    for key in keys:
        subset = [row for row in rows if str(row.get(field) or "") == key]
        output[key or "unknown"] = summarize(subset)
    return output


def compute_deltas(reports: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    ok = [report for report in reports if report.get("status") == "ok"]
    if len(ok) < 2:
        return {}
    baseline = next((report for report in ok if report["name"] == "always_use_rag"), ok[0])
    deltas: dict[str, dict[str, float]] = {}
    base_summary = baseline.get("summary") or {}
    for report in ok:
        if report is baseline:
            continue
        summary = report.get("summary") or {}
        deltas[f"{report['name']}_minus_{baseline['name']}"] = {
            metric: float(summary.get(metric, 0.0)) - float(base_summary.get(metric, 0.0))
            for metric in METRICS
        }
    return deltas


def write_report_files(output_dir: Path, report: dict[str, Any]) -> None:
    path = output_dir / f"{report['name']}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if report.get("cases"):
        write_rows_csv(output_dir / f"{report['name']}.csv", report["cases"])


def write_rows_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = sorted({key for row in rows for key in row.keys()})
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_summary_csv(path: Path, reports: list[dict[str, Any]]) -> None:
    fieldnames = [
        "name",
        "status",
        "mode",
        "keywords_enabled",
        "llm_enabled",
        "low_threshold",
        "high_threshold",
        "llm_accept_threshold",
        "cases",
        "positives",
        "negatives",
        "tp",
        "fp",
        "fn",
        "tn",
        *METRICS,
        "mcc",
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
                "mode": dims.get("mode", ""),
                "keywords_enabled": dims.get("keywords_enabled", ""),
                "llm_enabled": dims.get("llm_enabled", ""),
                "low_threshold": dims.get("low_threshold", ""),
                "high_threshold": dims.get("high_threshold", ""),
                "llm_accept_threshold": dims.get("llm_accept_threshold", ""),
                "duration_ms": report.get("duration_ms", ""),
                "error": report.get("error", ""),
            }
            for key in ["cases", "positives", "negatives", "tp", "fp", "fn", "tn", *METRICS, "mcc"]:
                row[key] = summary.get(key, "")
            writer.writerow(row)


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Security Router Only Report",
        "",
        f"- Run ID: `{payload['run_id']}`",
        f"- Testset: `{payload['testset']}`",
        f"- Cases: `{payload['case_count']}`",
        f"- Preset: `{payload['preset']}`",
        f"- Include LLM: `{payload['include_llm']}`",
        f"- Include no-keyword configs: `{payload['include_no_keywords']}`",
        f"- Limit cases: `{payload.get('limit_cases')}`",
        f"- Embedding device: `{payload.get('embedding_device') or 'env default'}`",
        "",
        "## Summary",
        "",
        "| config | status | kw | llm | low | high | acc | bal acc | precision | recall | specificity | fp | fn | p50 ms | p95 ms |",
        "| --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for report in payload["reports"]:
        dims = report.get("dimensions") or {}
        summary = report.get("summary") or {}
        lines.append(
            "| {name} | {status} | {kw} | {llm} | {low} | {high} | {acc} | {bal} | {prec} | {rec} | {spec} | {fp} | {fn} | {p50} | {p95} |".format(
                name=report["name"],
                status=report["status"],
                kw=_onoff(dims.get("keywords_enabled")),
                llm=_onoff(dims.get("llm_enabled")),
                low=_fmt(dims.get("low_threshold")),
                high=_fmt(dims.get("high_threshold")),
                acc=_fmt(summary.get("accuracy")),
                bal=_fmt(summary.get("balanced_accuracy")),
                prec=_fmt(summary.get("precision")),
                rec=_fmt(summary.get("recall")),
                spec=_fmt(summary.get("specificity")),
                fp=summary.get("fp", "-"),
                fn=summary.get("fn", "-"),
                p50=_fmt(summary.get("latency_p50_ms")),
                p95=_fmt(summary.get("latency_p95_ms")),
            )
        )

    lines.extend(["", "## Deltas Versus Baseline", ""])
    if payload["deltas"]:
        lines.append("| comparison | acc | bal acc | precision | recall | specificity | fpr | fnr |")
        lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
        for name, delta in payload["deltas"].items():
            lines.append(
                "| {name} | {acc} | {bal} | {prec} | {rec} | {spec} | {fpr} | {fnr} |".format(
                    name=name,
                    acc=_fmt(delta.get("accuracy"), signed=True),
                    bal=_fmt(delta.get("balanced_accuracy"), signed=True),
                    prec=_fmt(delta.get("precision"), signed=True),
                    rec=_fmt(delta.get("recall"), signed=True),
                    spec=_fmt(delta.get("specificity"), signed=True),
                    fpr=_fmt(delta.get("false_positive_rate"), signed=True),
                    fnr=_fmt(delta.get("false_negative_rate"), signed=True),
                )
            )
    else:
        lines.append("No comparable successful configs.")

    errors = [report for report in payload["reports"] if report.get("status") != "ok"]
    if errors:
        lines.extend(["", "## Errors", ""])
        for report in errors:
            lines.append(f"- `{report['name']}`: {report.get('error', 'unknown error')}")

    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This script only calls `router.route()` and never searches the vector database.",
            "- `specificity` is the negative-case block rate: `TN / (TN + FP)`.",
            "- `balanced_accuracy` is more useful than plain accuracy when positives and negatives are imbalanced.",
            "- Per-config JSON files include `false_positives` and `false_negatives` for quick router debugging.",
            "",
        ]
    )
    return "\n".join(lines)


def config_dimensions(config: RouterEvalConfig) -> dict[str, Any]:
    return {
        "mode": config.mode,
        "keywords_enabled": config.keywords_enabled,
        "llm_enabled": config.llm_enabled,
        "low_threshold": config.low_threshold,
        "high_threshold": config.high_threshold,
        "llm_accept_threshold": config.llm_accept_threshold,
    }


def parse_float_list(value: str) -> list[float]:
    output = []
    for item in str(value or "").split(","):
        item = item.strip()
        if not item:
            continue
        output.append(float(item))
    return output


def fmt_name(value: float) -> str:
    return f"{value:.2f}".replace(".", "")


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=100, method="inclusive")[max(0, min(99, p - 1))])


def mean(values) -> float:
    values = list(values)
    return float(sum(values) / len(values)) if values else 0.0


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


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except ValueError:
        return float(default)


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return int(default)
    try:
        return int(value)
    except ValueError:
        return int(default)


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
