from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Security RAG trace JSONL files.")
    parser.add_argument("--trace-dir", default="~/.claude/rag_traces")
    parser.add_argument("--date", default=None, help="Optional YYYY-MM-DD filter.")
    args = parser.parse_args()

    root = Path(args.trace_dir).expanduser()
    traces = load_traces(root, date=args.date)
    latencies = [float((trace.get("latency_ms") or {}).get("total") or 0.0) for trace in traces]
    route_counts = Counter(
        ((trace.get("router_decision") or {}).get("route") or "none")
        for trace in traces
    )
    source_counts = Counter(trace.get("source") or "unknown" for trace in traces)
    hit_counts = [len(trace.get("final_hits") or []) for trace in traces]
    error_counts = Counter(trace.get("error") or "ok" for trace in traces)
    summary = {
        "trace_dir": str(root),
        "traces": len(traces),
        "source_counts": dict(source_counts),
        "route_counts": dict(route_counts),
        "error_counts": dict(error_counts),
        "avg_hits": mean(hit_counts),
        "latency_avg_ms": mean(latencies),
        "latency_p50_ms": percentile(latencies, 50),
        "latency_p95_ms": percentile(latencies, 95),
        "latency_p99_ms": percentile(latencies, 99),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_traces(root: Path, *, date: str | None) -> list[dict]:
    if not root.exists():
        return []
    pattern = f"rag_traces_{date}.jsonl" if date else "rag_traces_*.jsonl"
    traces = []
    for path in sorted(root.glob(pattern)):
        with path.open("r", encoding="utf-8") as file:
            for line in file:
                if not line.strip():
                    continue
                try:
                    traces.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return traces


def mean(values) -> float:
    values = list(values)
    return float(sum(values) / len(values)) if values else 0.0


def percentile(values: list[float], p: int) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    return float(statistics.quantiles(values, n=100, method="inclusive")[max(0, min(99, p - 1))])


if __name__ == "__main__":
    raise SystemExit(main())
