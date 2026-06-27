#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.swebench_adapter import (  # noqa: E402
    DEFAULT_GIT_TIMEOUT_SECONDS,
    DEFAULT_SWEBENCH_REPO_CACHE_ROOT,
    DEFAULT_SWEBENCH_SPLIT,
    DEFAULT_SWEBENCH_VERIFIED_DATASET,
    dataset_name_candidates,
    load_swebench_instances,
    official_evaluation_command,
    prediction_record,
    run_swebench_instance,
)


DEFAULT_EVAL_ROOT = Path(".evals/swebench_verified")
DEFAULT_WORKSPACE_ROOT = Path(".evals/swebench_verified_workspaces")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run a small batch from SWE-bench Verified through the coding runtime.",
    )
    parser.add_argument(
        "--dataset-name",
        default=DEFAULT_SWEBENCH_VERIFIED_DATASET,
        help="Hugging Face dataset name. Aliases: verified, swe-verified.",
    )
    parser.add_argument("--split", default=DEFAULT_SWEBENCH_SPLIT)
    parser.add_argument("--limit", type=int, default=10, help="Number of instances to run when --instance-id is not provided.")
    parser.add_argument("--offset", type=int, default=0, help="Start offset for deterministic dataset slicing.")
    parser.add_argument(
        "--instance-id",
        action="append",
        default=[],
        help="Specific instance id. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--instances-file",
        default="",
        help=(
            "Local JSON/JSONL SWE-bench records file. When provided, the script "
            "selects instances from this file instead of contacting Hugging Face."
        ),
    )
    parser.add_argument("--eval-root", default=str(DEFAULT_EVAL_ROOT))
    parser.add_argument("--workspace-root", default=str(DEFAULT_WORKSPACE_ROOT))
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--max-reasoning-steps", type=int, default=80)
    parser.add_argument("--reuse-workspace", action="store_true")
    parser.add_argument("--repo-cache-root", default=str(DEFAULT_SWEBENCH_REPO_CACHE_ROOT))
    parser.add_argument("--clone-retries", type=int, default=2)
    parser.add_argument("--git-timeout-seconds", type=int, default=DEFAULT_GIT_TIMEOUT_SECONDS)
    parser.add_argument("--dry-run", action="store_true", help="Only select and print the 10 instances.")
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first failed agent run.")
    parser.add_argument(
        "--swebench-repo",
        default="",
        help="Optional path to the official SWE-bench repo for evaluation command generation.",
    )
    parser.add_argument(
        "--evaluate",
        action="store_true",
        help="Run official SWE-bench harness once after all predictions are written.",
    )
    parser.add_argument("--official-max-workers", type=int, default=1)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    instance_ids = parse_instance_ids(args.instance_id)
    dataset_for_harness = dataset_name_candidates(args.dataset_name)[0]
    batch_id = "swe_verified_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    batch_dir = Path(args.eval_root) / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)
    task_eval_root = batch_dir / "tasks"
    task_workspace_root = Path(args.workspace_root) / batch_id
    predictions_path = batch_dir / "predictions.jsonl"

    instances = load_swebench_instances(
        dataset_name=args.dataset_name,
        split=args.split,
        instance_ids=instance_ids,
        limit=args.limit,
        offset=args.offset,
        records_path=args.instances_file or None,
    )
    write_json(batch_dir / "selected_instances.json", [instance.__dict__ for instance in instances])

    if args.dry_run:
        for index, instance in enumerate(instances, start=1):
            print(f"[{index:02d}/{len(instances):02d}] {instance.instance_id} repo={instance.repo} base={instance.base_commit}")
        payload = build_payload(
            batch_id=batch_id,
            batch_dir=batch_dir,
            args=args,
            rows=[],
            instances=instances,
            predictions_path=predictions_path,
            official_command=[],
            dry_run=True,
        )
        write_batch_reports(batch_dir, payload)
        print(f"wrote SWE-bench Verified dry-run report: {batch_dir / 'summary.md'}")
        return 0

    rows: list[dict[str, Any]] = []
    predictions_path.write_text("", encoding="utf-8")
    for index, instance in enumerate(instances, start=1):
        if not args.quiet:
            print(f"[{index:02d}/{len(instances):02d}] START {instance.instance_id} repo={instance.repo}", flush=True)
        started = time.perf_counter()
        try:
            result = run_swebench_instance(
                instance=instance,
                eval_root=task_eval_root,
                workspace_root=task_workspace_root,
                model_name=args.model_name,
                max_reasoning_steps=args.max_reasoning_steps,
                reuse_workspace=args.reuse_workspace,
                progress=None,
                swebench_repo=None,
                evaluate=False,
                dataset_name=dataset_for_harness,
                repo_cache_root=args.repo_cache_root or None,
                clone_retries=args.clone_retries,
                git_timeout_seconds=args.git_timeout_seconds,
            )
            metrics = read_metrics(result.run_dir)
            append_prediction(
                predictions_path,
                instance_id=result.instance.instance_id,
                model_name_or_path=prediction_model_name(args.model_name, metrics),
                model_patch=result.model_patch,
            )
            row = {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "status": "pass" if result.run_state.status == "completed" else "fail",
                "run_status": result.run_state.status,
                "error": "",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "workspace": str(result.workspace),
                "eval_dir": str(result.eval_dir),
                "run_dir": str(result.run_dir),
                "predictions_path": str(result.predictions_path),
                "patch_bytes": len(result.model_patch.encode("utf-8")),
                "metrics": metrics,
            }
        except Exception as exc:
            row = {
                "instance_id": instance.instance_id,
                "repo": instance.repo,
                "base_commit": instance.base_commit,
                "status": "error",
                "run_status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "workspace": "",
                "eval_dir": "",
                "run_dir": "",
                "predictions_path": "",
                "patch_bytes": 0,
                "metrics": {},
            }
        rows.append(row)
        print_task_result(index, len(instances), row, quiet=args.quiet)
        write_batch_reports(
            batch_dir,
            build_payload(
                batch_id=batch_id,
                batch_dir=batch_dir,
                args=args,
                rows=rows,
                instances=instances,
                predictions_path=predictions_path,
                official_command=[],
                dry_run=False,
            ),
        )
        if args.fail_fast and row["status"] != "pass":
            break

    official_command = []
    if args.swebench_repo:
        official_command = official_evaluation_command(
            swebench_repo=args.swebench_repo,
            dataset_name=dataset_for_harness,
            predictions_path=predictions_path,
            run_id=batch_id,
            instance_ids=[row["instance_id"] for row in rows if row.get("patch_bytes", 0) > 0],
            max_workers=args.official_max_workers,
        )
    payload = build_payload(
        batch_id=batch_id,
        batch_dir=batch_dir,
        args=args,
        rows=rows,
        instances=instances,
        predictions_path=predictions_path,
        official_command=official_command,
        dry_run=False,
    )
    write_batch_reports(batch_dir, payload)

    if args.evaluate:
        if not args.swebench_repo:
            raise SystemExit("--evaluate requires --swebench-repo")
        if not official_command:
            raise SystemExit("No official evaluation command was generated.")
        print("running official SWE-bench evaluation:", " ".join(official_command), flush=True)
        subprocess.run(official_command, cwd=Path(args.swebench_repo), check=True)

    print(f"wrote SWE-bench Verified batch report: {batch_dir / 'summary.md'}")
    return 1 if any(row["status"] == "error" for row in rows) else 0


def build_payload(
    *,
    batch_id: str,
    batch_dir: Path,
    args: argparse.Namespace,
    rows: list[dict[str, Any]],
    instances: list,
    predictions_path: Path,
    official_command: list[str],
    dry_run: bool,
) -> dict[str, Any]:
    summary = summarize_rows(rows)
    return {
        "schema_version": 1,
        "batch_id": batch_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "dry_run": bool(dry_run),
        "dataset": {
            "name": args.dataset_name,
            "resolved_name": dataset_name_candidates(args.dataset_name)[0],
            "split": args.split,
            "limit": args.limit,
            "offset": args.offset,
            "selected_count": len(instances),
            "instances_file": args.instances_file or "",
        },
        "runtime": {
            "model_name": args.model_name or "",
            "max_reasoning_steps": args.max_reasoning_steps,
            "reuse_workspace": bool(args.reuse_workspace),
            "repo_cache_root": args.repo_cache_root,
            "clone_retries": args.clone_retries,
            "git_timeout_seconds": args.git_timeout_seconds,
        },
        "batch_dir": str(batch_dir),
        "predictions_path": str(predictions_path),
        "official_eval_command": official_command,
        "summary": summary,
        "rows": rows,
    }


def summarize_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    passed = sum(1 for row in rows if row.get("status") == "pass")
    errors = sum(1 for row in rows if row.get("status") == "error")
    failed = total - passed - errors
    total_tokens = sum(int((row.get("metrics") or {}).get("total_tokens") or 0) for row in rows)
    tool_calls = sum(int((row.get("metrics") or {}).get("tool_calls") or 0) for row in rows)
    model_calls = sum(int((row.get("metrics") or {}).get("model_calls") or 0) for row in rows)
    return {
        "total": total,
        "agent_passed": passed,
        "agent_failed": failed,
        "errors": errors,
        "agent_pass_rate": passed / total if total else 0.0,
        "patches_written": sum(1 for row in rows if int(row.get("patch_bytes") or 0) > 0),
        "total_tokens": total_tokens,
        "tool_calls": tool_calls,
        "model_calls": model_calls,
        "wall_duration_ms": round(sum(float(row.get("duration_ms") or 0) for row in rows), 3),
    }


def write_batch_reports(batch_dir: Path, payload: dict[str, Any]) -> None:
    write_json(batch_dir / "summary.json", payload)
    write_json(batch_dir / "rows.json", payload.get("rows") or [])
    (batch_dir / "summary.md").write_text(render_markdown(payload), encoding="utf-8")


def render_markdown(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    dataset = payload.get("dataset") or {}
    rows = payload.get("rows") or []
    lines = [
        "# SWE-bench Verified Batch",
        "",
        "## Summary",
        "",
        f"- Batch ID: `{payload.get('batch_id', '')}`",
        f"- Dataset: `{dataset.get('resolved_name') or dataset.get('name')}` / `{dataset.get('split')}`",
        f"- Selected: {dataset.get('selected_count', 0)}",
        f"- Agent passed: {summary.get('agent_passed', 0)}/{summary.get('total', 0)}",
        f"- Agent pass rate: {summary.get('agent_pass_rate', 0):.2%}",
        f"- Patches written: {summary.get('patches_written', 0)}",
        f"- Total tokens: {summary.get('total_tokens', 0)}",
        f"- Tool calls: {summary.get('tool_calls', 0)}",
        f"- Model calls: {summary.get('model_calls', 0)}",
        f"- Predictions: `{payload.get('predictions_path', '')}`",
        "",
        "## Official Evaluation",
        "",
    ]
    command = payload.get("official_eval_command") or []
    if command:
        lines.extend(["```bash", " ".join(command), "```", ""])
    else:
        lines.extend(["_No official evaluation command generated. Pass `--swebench-repo` to generate one._", ""])
    lines.extend([
        "## Rows",
        "",
        "| # | instance | repo | status | patch bytes | tokens | tools | duration | error |",
        "| ---: | --- | --- | --- | ---: | ---: | ---: | ---: | --- |",
    ])
    for index, row in enumerate(rows, start=1):
        metrics = row.get("metrics") or {}
        lines.append(
            "| "
            f"{index} | "
            f"`{row.get('instance_id', '')}` | "
            f"{row.get('repo', '')} | "
            f"{row.get('status', '')} | "
            f"{row.get('patch_bytes', 0)} | "
            f"{metrics.get('total_tokens', 0)} | "
            f"{metrics.get('tool_calls', 0)} | "
            f"{float(row.get('duration_ms') or 0):.0f}ms | "
            f"{escape_cell(row.get('error', ''))} |"
        )
    return "\n".join(lines).rstrip() + "\n"


def append_prediction(
    path: Path,
    *,
    instance_id: str,
    model_name_or_path: str,
    model_patch: str,
) -> None:
    payload = prediction_record(
        instance_id=instance_id,
        model_name_or_path=model_name_or_path,
        model_patch=model_patch,
    )
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(payload, ensure_ascii=False) + "\n")


def read_metrics(run_dir: str | Path) -> dict[str, Any]:
    path = Path(run_dir) / "metrics.json"
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def prediction_model_name(explicit_model_name: str | None, metrics: dict[str, Any]) -> str:
    if explicit_model_name:
        return explicit_model_name
    models = [str(item) for item in (metrics.get("models") or []) if str(item).strip()]
    if models:
        return ",".join(models)
    return "coding-runtime"


def print_task_result(index: int, total: int, row: dict[str, Any], *, quiet: bool) -> None:
    if quiet:
        return
    marker = row.get("status", "").upper()
    metrics = row.get("metrics") or {}
    reason = f" error={row['error']}" if row.get("error") else ""
    print(
        f"[{index:02d}/{total:02d}] {marker} {row.get('instance_id', '')} "
        f"patch={row.get('patch_bytes', 0)}B "
        f"tokens={metrics.get('total_tokens', 0)} "
        f"tools={metrics.get('tool_calls', 0)} "
        f"time={float(row.get('duration_ms') or 0):.0f}ms{reason}",
        flush=True,
    )


def parse_instance_ids(values: list[str]) -> list[str]:
    ids: list[str] = []
    for value in values:
        ids.extend(item.strip() for item in str(value).split(",") if item.strip())
    return ids


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def escape_cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


if __name__ == "__main__":
    raise SystemExit(main())
