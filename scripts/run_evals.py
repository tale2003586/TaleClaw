from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.harness import run_coding_benchmark


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agent evaluation suites.")
    parser.add_argument(
        "--suite",
        choices=["coding"],
        default="coding",
        help="Evaluation suite to run.",
    )
    parser.add_argument(
        "--benchmark-path",
        default="benchmarks/coding_tasks.json",
        help="Benchmark task JSON path.",
    )
    parser.add_argument(
        "--eval-root",
        default=".evals/runs",
        help="Directory for evaluation run artifacts.",
    )
    parser.add_argument(
        "--workspace-root",
        default="",
        help="Optional directory for temporary copied fixture workspaces.",
    )
    parser.add_argument(
        "--runner",
        choices=["scripted", "real"],
        default="scripted",
        help="Use deterministic scripted responses or the configured real coding provider.",
    )
    parser.add_argument(
        "--task-id",
        default="",
        help="Run only one benchmark task by id.",
    )
    parser.add_argument(
        "--keep-workspace",
        action="store_true",
        help="Keep copied fixture workspaces for debugging.",
    )
    parser.add_argument(
        "--no-step-budget",
        action="store_true",
        help="Disable benchmark step-budget pass/fail checks and use a large runtime guard.",
    )
    parser.add_argument(
        "--max-reasoning-steps",
        type=int,
        default=None,
        help="Override the benchmark reasoning-step limit for each task.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Disable line-by-line progress output.",
    )
    args = parser.parse_args()

    if args.suite != "coding":
        raise ValueError(f"Unsupported suite: {args.suite}")

    payload = run_coding_benchmark(
        benchmark_path=Path(args.benchmark_path),
        eval_root=Path(args.eval_root),
        workspace_root=Path(args.workspace_root) if args.workspace_root else None,
        runner_mode=args.runner,
        task_id=args.task_id or None,
        keep_workspace=args.keep_workspace,
        no_step_budget=args.no_step_budget,
        max_reasoning_steps=args.max_reasoning_steps,
        progress=None if args.quiet else render_progress,
    )
    print(json.dumps({
        "eval_id": payload["eval_id"],
        "runner": payload["runner"],
        "workspace_root": payload["workspace_root"],
        "workspace_retained": payload["workspace_retained"],
        "summary": payload["summary"],
    }, indent=2, ensure_ascii=False))


def render_progress(event: dict) -> None:
    name = event.get("event")
    if name == "eval_started":
        print(
            f"[eval] {event['eval_id']} runner={event['runner_mode']} "
            f"tasks={event['task_count']} workspace={event['workspace_root']}",
            flush=True,
        )
    elif name == "task_started":
        print(
            f"[{event['index']:02d}/{event['total']:02d}] START "
            f"{event['id']} ({event['category']})",
            flush=True,
        )
    elif name == "task_finished":
        marker = "PASS" if event.get("status") == "pass" else "FAIL"
        reason = f" reason={event['failure_reason']}" if event.get("failure_reason") else ""
        print(
            f"[{event['index']:02d}/{event['total']:02d}] {marker}  "
            f"{event['id']} steps={event['reasoning_steps']} "
            f"tools={event['tool_calls']} time={event['duration_ms']:.0f}ms{reason}",
            flush=True,
        )
    elif name == "eval_finished":
        summary = event["summary"]
        print(
            f"[eval] DONE {event['eval_id']} pass={summary['passed']}/"
            f"{summary['total_tasks']} rate={summary['pass_rate']:.2%} "
            f"artifacts={event['eval_dir']}",
            flush=True,
        )


if __name__ == "__main__":
    main()
