from __future__ import annotations

import argparse
import json
import sys
from dataclasses import fields
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from runtime.trace.index_store import TraceIndexStore
from runtime.trace.run_state import RunState
from runtime.trace.summary import write_trace_summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Index existing trace run directories into trace_runs/trace_steps."
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Directory to scan recursively. Can be passed multiple times. Defaults to .runs and .evals.",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="Optional PostgreSQL DSN for the trace index.",
    )
    parser.add_argument(
        "--skip-summary-build",
        action="store_true",
        help="Do not build missing trace_summary.json files.",
    )
    args = parser.parse_args()

    roots = [Path(item) for item in args.root] or [Path(".runs"), Path(".evals")]
    index = TraceIndexStore(args.database_url, default_root=Path(".runs"))
    count = 0
    skipped = 0
    for run_dir in _find_run_dirs(roots):
        run_state_payload = _read_json(run_dir / "run_state.json")
        if not run_state_payload:
            skipped += 1
            continue
        if not (run_dir / "trace_summary.json").exists() and not args.skip_summary_build:
            try:
                write_trace_summary(run_dir)
            except Exception:
                pass
        run_state = _run_state_from_dict(run_state_payload)
        report_payload = _read_json(run_dir / "report.json")
        report = report_payload.get("report", {}) if isinstance(report_payload.get("report"), dict) else {}
        metrics = _read_json(run_dir / "metrics.json")
        summary = _read_json(run_dir / "trace_summary.json")
        index.upsert_run(
            run_state,
            run_dir=run_dir,
            report=report,
            summary=summary,
            metrics=metrics,
        )
        execution_path = summary.get("execution_path")
        if isinstance(execution_path, list):
            index.replace_steps(run_state.run_id, execution_path)
        count += 1
    print(json.dumps({"indexed": count, "skipped": skipped}, ensure_ascii=False))
    return 0


def _find_run_dirs(roots: list[Path]) -> list[Path]:
    run_dirs: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("run_state.json"):
            run_dir = path.parent
            if (run_dir / "trace.jsonl").exists():
                run_dirs.append(run_dir)
    return sorted(set(run_dirs))


def _run_state_from_dict(payload: dict[str, Any]) -> RunState:
    allowed = {field.name for field in fields(RunState)}
    values = {key: value for key, value in payload.items() if key in allowed}
    return RunState(**values)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
