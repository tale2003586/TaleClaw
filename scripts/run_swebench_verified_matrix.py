#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
RUN_SWEBENCH_VERIFIED = ROOT / "scripts" / "run_swebench_verified.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation.swebench_adapter import (  # noqa: E402
    DEFAULT_GIT_TIMEOUT_SECONDS,
    DEFAULT_SWEBENCH_REPO_CACHE_ROOT,
    DEFAULT_SWEBENCH_SPLIT,
    DEFAULT_SWEBENCH_VERIFIED_DATASET,
)
from scripts.run_coding_agent_matrix import (  # noqa: E402
    RATIO_METRIC_KEYS,
    SUM_METRIC_KEYS,
    dimension_text,
    divide,
    env_value,
    escape_cell,
    expand_agent_feature_cells,
    expand_variant_cells,
    format_duration,
    format_value,
    merge_dicts,
    merge_env,
    normalize_number,
    numeric,
    slug,
    unique_id,
)


DEFAULT_OUTPUT_ROOT = ".evals/swebench_verified_matrix"

DEFAULT_METRICS = [
    "pass_rate",
    "agent_pass_rate",
    "patches_written",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "wall_duration_ms",
    "avg_total_tokens_per_instance",
    "avg_tool_calls_per_instance",
    "tool_calls",
    "model_calls",
    "tool_failures",
    "tool_denials",
    "context_build_compressed_count",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run SWE-bench Verified batches as an agent/feature matrix.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Verified matrix config JSON. If omitted, a one-cell default matrix is used.",
    )
    parser.add_argument(
        "--output-root",
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory where matrix artifacts are written.",
    )
    parser.add_argument("--dataset-name", default="", help="Override config dataset name.")
    parser.add_argument("--split", default="", help="Override config dataset split.")
    parser.add_argument("--limit", type=int, default=-1, help="Override config task limit.")
    parser.add_argument("--offset", type=int, default=-1, help="Override config task offset.")
    parser.add_argument(
        "--instance-id",
        action="append",
        default=[],
        help="Run only these SWE-bench instance ids. May be repeated or comma-separated.",
    )
    parser.add_argument(
        "--instances-file",
        default="",
        help="Override config local SWE-bench records JSON/JSONL file.",
    )
    parser.add_argument(
        "--max-reasoning-steps",
        type=int,
        default=0,
        help="Override per-cell max reasoning steps.",
    )
    parser.add_argument(
        "--limit-cells",
        type=int,
        default=0,
        help="Run only the first N expanded cells.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Write the expanded matrix plan and report without running Verified tasks.",
    )
    parser.add_argument("--fail-fast", action="store_true", help="Stop after the first error cell.")
    parser.add_argument(
        "--fail-on-agent-fail",
        action="store_true",
        help="Exit non-zero when any completed cell has agent failed instances.",
    )
    parser.add_argument("--quiet", action="store_true", help="Suppress per-cell progress output.")
    parser.add_argument(
        "--write-template",
        default="",
        help="Write an editable example SWE-bench Verified matrix config JSON to this path and exit.",
    )
    args = parser.parse_args(argv)

    if args.write_template:
        path = Path(args.write_template)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(template_config(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        print(f"wrote template config: {path}")
        return 0

    config = load_config(Path(args.config)) if args.config else default_config()
    apply_cli_overrides(config, args)

    matrix_id = "swe_verified_matrix_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    matrix_root = (Path(args.output_root) / matrix_id).resolve()
    matrix_root.mkdir(parents=True, exist_ok=True)

    cells = expand_cells(config)
    if args.limit_cells > 0:
        cells = cells[: args.limit_cells]

    payload: dict[str, Any] = {
        "schema_version": 1,
        "matrix_id": matrix_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "matrix_root": str(matrix_root),
        "config": config,
        "dry_run": bool(args.dry_run),
        "cells": [],
        "summary": {},
    }
    write_json(matrix_root / "expanded_plan.json", {"cells": cells, "config": config})

    for index, cell in enumerate(cells, start=1):
        if not args.quiet:
            print(
                f"[{index:02d}/{len(cells):02d}] {cell['cell_id']} "
                f"agent={cell['agent_name']} feature={cell['feature_set_name']}",
                flush=True,
            )
        result = (
            dry_run_cell(cell, matrix_root=matrix_root, config=config)
            if args.dry_run
            else run_cell(cell, matrix_root=matrix_root, config=config, quiet=args.quiet)
        )
        payload["cells"].append(result)
        write_reports(matrix_root, payload, metrics=config.get("metrics") or DEFAULT_METRICS)
        if args.fail_fast and result["status"] == "error":
            break

    payload["summary"] = summarize_matrix(payload["cells"])
    write_reports(matrix_root, payload, metrics=config.get("metrics") or DEFAULT_METRICS)

    if not args.quiet:
        summary = payload["summary"]
        print(
            f"[verified-matrix] DONE cells={summary['cells']} "
            f"pass={summary['passed_cells']} fail={summary['failed_cells']} "
            f"error={summary['error_cells']} artifacts={matrix_root}",
            flush=True,
        )
    print(f"wrote SWE-bench Verified matrix report: {matrix_root / 'report.md'}")

    if payload["summary"].get("error_cells", 0):
        return 1
    if args.fail_on_agent_fail and payload["summary"].get("failed_cells", 0):
        return 1
    return 0


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise SystemExit("SWE-bench Verified matrix config must be a JSON object.")
    schema_version = int(payload.get("schema_version", 1))
    if schema_version != 1:
        raise SystemExit(f"Unsupported matrix config schema_version: {schema_version}")
    return payload


def default_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "swebench-verified-matrix-default",
        "swebench": default_swebench_config(),
        "repetitions": 1,
        "env": {
            "LLM_HEALTHCHECK_ON_STARTUP": "0",
        },
        "agents": [
            {
                "name": "real-default",
                "enabled": True,
                "env": {
                    "LLM_ROUTE_CODING": "openai_relay,deepseek,mimo",
                },
                "dimensions": {
                    "agent": "real",
                    "model_route": "default",
                },
            }
        ],
        "feature_sets": [
            {
                "name": "budget-on",
                "env": {
                    "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                    "WORKING_MEMORY_RESUME_ENABLED": "1",
                },
                "harness": {
                    "max_reasoning_steps": 80,
                },
                "dimensions": {
                    "context_budget": "on",
                    "working_memory_resume": "on",
                },
            }
        ],
        "metrics": DEFAULT_METRICS,
    }


def template_config() -> dict[str, Any]:
    config = default_config()
    config["name"] = "swebench-verified-matrix-template"
    config["notes"] = [
        "agents and feature_sets are crossed into matrix cells.",
        "Each cell runs scripts/run_swebench_verified.py in a fresh subprocess, so env flags affect import-time config constants.",
        "Start with limit=1 or explicit instance_ids before spending tokens on 10 tasks x multiple cells.",
        "PASS means the agent completed and wrote a patch; official SWE-bench resolved status requires the official harness.",
    ]
    config["swebench"] = {
        **default_swebench_config(),
        "limit": 1,
        "offset": 0,
        "instance_ids": [],
    }
    config["feature_sets"].append({
        "name": "context-budget-off",
        "enabled": True,
        "env": {
            "CONTEXT_ENABLE_SECTION_BUDGET": "0",
            "WORKING_MEMORY_RESUME_ENABLED": "1",
        },
        "harness": {
            "max_reasoning_steps": 80,
        },
        "dimensions": {
            "context_budget": "off",
            "working_memory_resume": "on",
        },
    })
    config["feature_sets"].append({
        "name": "working-memory-resume-off",
        "enabled": False,
        "env": {
            "CONTEXT_ENABLE_SECTION_BUDGET": "1",
            "WORKING_MEMORY_RESUME_ENABLED": "0",
        },
        "harness": {
            "max_reasoning_steps": 80,
        },
        "dimensions": {
            "context_budget": "on",
            "working_memory_resume": "off",
        },
    })
    config["agents"].append({
        "name": "real-alt-route",
        "enabled": False,
        "env": {
            "LLM_ROUTE_CODING": "deepseek,mimo,openai_relay",
        },
        "dimensions": {
            "agent": "real",
            "model_route": "alt",
        },
    })
    return config


def default_swebench_config() -> dict[str, Any]:
    return {
        "dataset_name": DEFAULT_SWEBENCH_VERIFIED_DATASET,
        "split": DEFAULT_SWEBENCH_SPLIT,
        "limit": 10,
        "offset": 0,
        "instance_ids": [],
        "instances_file": "",
        "model_name": "",
        "max_reasoning_steps": 80,
        "reuse_workspace": False,
        "repo_cache_root": str(DEFAULT_SWEBENCH_REPO_CACHE_ROOT),
        "clone_retries": 2,
        "git_timeout_seconds": DEFAULT_GIT_TIMEOUT_SECONDS,
        "swebench_repo": "",
        "evaluate": False,
        "official_max_workers": 1,
    }


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
    swebench = dict(config.get("swebench") or {})
    if args.dataset_name:
        swebench["dataset_name"] = args.dataset_name
    if args.split:
        swebench["split"] = args.split
    if args.limit >= 0:
        swebench["limit"] = args.limit
    if args.offset >= 0:
        swebench["offset"] = args.offset
    instance_ids = parse_ids(args.instance_id)
    if instance_ids:
        swebench["instance_ids"] = instance_ids
    if args.instances_file:
        swebench["instances_file"] = args.instances_file
    if args.max_reasoning_steps > 0:
        swebench["max_reasoning_steps"] = args.max_reasoning_steps
    config["swebench"] = swebench


def expand_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    repetitions = max(1, int(config.get("repetitions") or 1))
    if config.get("variants"):
        base_cells = expand_variant_cells(config)
    else:
        base_cells = expand_agent_feature_cells(config)

    cells = []
    seen_ids: set[str] = set()
    for base in base_cells:
        for repetition in range(1, repetitions + 1):
            cell = deepcopy(base)
            cell["repetition"] = repetition
            cell.setdefault("dimensions", {})
            if repetitions > 1:
                cell["dimensions"]["repetition"] = str(repetition)
            stem = cell["cell_id"]
            if repetitions > 1:
                stem += f"__r{repetition}"
            cell["cell_id"] = unique_id(stem, seen_ids)
            cells.append(cell)
    if not cells:
        raise SystemExit("SWE-bench Verified matrix config expanded to zero cells.")
    return cells


def run_cell(
    cell: dict[str, Any],
    *,
    matrix_root: Path,
    config: dict[str, Any],
    quiet: bool,
) -> dict[str, Any]:
    cell_dir = matrix_root / "cells" / cell["cell_id"]
    eval_root = cell_dir / "evals"
    workspace_root = cell_dir / "workspaces"
    cell_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    for key in cell.get("env_unset") or []:
        env.pop(str(key), None)
    for key, value in (cell.get("env") or {}).items():
        if value is None:
            env.pop(str(key), None)
        else:
            env[str(key)] = env_value(value)

    swebench = cell_swebench_config(config, cell)
    cmd = build_verified_command(
        swebench=swebench,
        eval_root=eval_root,
        workspace_root=workspace_root,
        quiet=True,
        extra_args=cell.get("extra_args") or [],
    )

    started = time.perf_counter()
    timeout = int(cell.get("timeout_seconds") or 0) or None
    try:
        completed = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
        process_error = ""
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = exc.stdout or ""
        stderr = exc.stderr or ""
        process_error = f"timeout after {timeout} seconds"

    wall_duration_ms = round((time.perf_counter() - started) * 1000, 3)
    write_text(cell_dir / "stdout.txt", stdout)
    write_text(cell_dir / "stderr.txt", stderr)
    write_json(
        cell_dir / "command.json",
        {
            "cmd": cmd,
            "env_overrides": cell.get("env") or {},
            "env_unset": cell.get("env_unset") or [],
            "swebench": swebench,
            "returncode": returncode,
            "process_error": process_error,
            "wall_duration_ms": wall_duration_ms,
        },
    )

    batch_payload = load_single_batch_payload(eval_root)
    result = build_cell_result(
        cell,
        cell_dir=cell_dir,
        batch_payload=batch_payload,
        returncode=returncode,
        process_error=process_error,
        wall_duration_ms=wall_duration_ms,
    )
    write_json(cell_dir / "cell.json", result)

    if not quiet:
        marker = result["status"].upper()
        metrics = result.get("metrics") or {}
        print(
            f"    {marker} pass_rate={metrics.get('pass_rate', 0):.2%} "
            f"tokens={metrics.get('total_tokens', 0)} "
            f"tools={metrics.get('tool_calls', 0)} "
            f"wall={wall_duration_ms:.0f}ms",
            flush=True,
        )
        print_instance_results(result)
    return result


def dry_run_cell(cell: dict[str, Any], *, matrix_root: Path, config: dict[str, Any]) -> dict[str, Any]:
    cell_dir = matrix_root / "cells" / cell["cell_id"]
    cell_dir.mkdir(parents=True, exist_ok=True)
    swebench = cell_swebench_config(config, cell)
    cmd = build_verified_command(
        swebench=swebench,
        eval_root=cell_dir / "evals",
        workspace_root=cell_dir / "workspaces",
        quiet=True,
        extra_args=cell.get("extra_args") or [],
    )
    write_json(
        cell_dir / "command.json",
        {
            "cmd": cmd,
            "env_overrides": cell.get("env") or {},
            "env_unset": cell.get("env_unset") or [],
            "swebench": swebench,
            "dry_run": True,
        },
    )
    result = build_cell_result(
        cell,
        cell_dir=cell_dir,
        batch_payload={},
        returncode=0,
        process_error="",
        wall_duration_ms=0,
        dry_run=True,
    )
    write_json(cell_dir / "cell.json", result)
    return result


def cell_swebench_config(config: dict[str, Any], cell: dict[str, Any]) -> dict[str, Any]:
    return merge_dicts(
        default_swebench_config(),
        config.get("swebench") or {},
        config.get("harness_defaults") or {},
        cell.get("harness") or {},
    )


def build_verified_command(
    *,
    swebench: dict[str, Any],
    eval_root: Path,
    workspace_root: Path,
    quiet: bool,
    extra_args: list[Any],
) -> list[str]:
    cmd = [
        sys.executable,
        str(RUN_SWEBENCH_VERIFIED),
        "--dataset-name",
        str(swebench.get("dataset_name") or DEFAULT_SWEBENCH_VERIFIED_DATASET),
        "--split",
        str(swebench.get("split") or DEFAULT_SWEBENCH_SPLIT),
        "--eval-root",
        str(eval_root),
        "--workspace-root",
        str(workspace_root),
        "--limit",
        str(int(swebench.get("limit") or 0)),
        "--offset",
        str(int(swebench.get("offset") or 0)),
        "--max-reasoning-steps",
        str(int(swebench.get("max_reasoning_steps") or 80)),
        "--repo-cache-root",
        str(swebench.get("repo_cache_root") or DEFAULT_SWEBENCH_REPO_CACHE_ROOT),
        "--clone-retries",
        str(int(swebench.get("clone_retries") or 0)),
        "--git-timeout-seconds",
        str(int(swebench.get("git_timeout_seconds") or DEFAULT_GIT_TIMEOUT_SECONDS)),
        "--official-max-workers",
        str(int(swebench.get("official_max_workers") or 1)),
    ]
    for instance_id in parse_ids(swebench.get("instance_ids") or []):
        cmd.extend(["--instance-id", instance_id])
    if swebench.get("instances_file"):
        cmd.extend(["--instances-file", str(swebench["instances_file"])])
    if swebench.get("model_name"):
        cmd.extend(["--model-name", str(swebench["model_name"])])
    if truthy(swebench.get("reuse_workspace")):
        cmd.append("--reuse-workspace")
    if swebench.get("swebench_repo"):
        cmd.extend(["--swebench-repo", str(swebench["swebench_repo"])])
    if truthy(swebench.get("evaluate")):
        cmd.append("--evaluate")
    if quiet:
        cmd.append("--quiet")
    cmd.extend(str(item) for item in extra_args)
    return cmd


def load_single_batch_payload(eval_root: Path) -> dict[str, Any]:
    batch_dirs = sorted(path for path in eval_root.glob("swe_verified_*") if path.is_dir())
    if not batch_dirs:
        return {}
    latest = batch_dirs[-1]
    summary_path = latest / "summary.json"
    if not summary_path.exists():
        return {"batch_dir": str(latest)}
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"batch_dir": str(latest)}
    payload["batch_dir"] = str(latest)
    return payload if isinstance(payload, dict) else {"batch_dir": str(latest)}


def build_cell_result(
    cell: dict[str, Any],
    *,
    cell_dir: Path,
    batch_payload: dict[str, Any],
    returncode: int,
    process_error: str,
    wall_duration_ms: float,
    dry_run: bool = False,
) -> dict[str, Any]:
    rows = list(batch_payload.get("rows") or [])
    batch_summary = dict(batch_payload.get("summary") or {})
    run_metrics = collect_run_metrics(rows)
    instance_count = int(batch_summary.get("total") or len(rows) or 0)
    metrics = {
        **batch_summary,
        **run_metrics,
        "pass_rate": numeric(batch_summary.get("agent_pass_rate")),
        "agent_pass_rate": numeric(batch_summary.get("agent_pass_rate")),
        "wall_duration_ms": wall_duration_ms,
        "avg_total_tokens_per_instance": divide(run_metrics.get("total_tokens", 0), instance_count),
        "avg_input_tokens_per_instance": divide(run_metrics.get("input_tokens", 0), instance_count),
        "avg_output_tokens_per_instance": divide(run_metrics.get("output_tokens", 0), instance_count),
        "avg_tool_calls_per_instance": divide(run_metrics.get("tool_calls", 0), instance_count),
        "avg_model_calls_per_instance": divide(run_metrics.get("model_calls", 0), instance_count),
        "avg_wall_duration_ms_per_instance": divide(wall_duration_ms, instance_count),
    }
    if dry_run:
        status = "planned"
    elif returncode != 0 or process_error or not batch_payload:
        status = "error"
    elif int(batch_summary.get("errors") or 0) > 0:
        status = "error"
    elif int(batch_summary.get("agent_failed") or 0) > 0:
        status = "fail"
    else:
        status = "pass"

    instance_rows = collect_instance_rows(rows)
    failed_instances = [
        {
            "id": task.get("id", ""),
            "repo": task.get("repo", ""),
            "status": task.get("status", ""),
            "error": task.get("error", ""),
            "run_dir": task.get("run_dir", ""),
        }
        for task in instance_rows
        if not task.get("passed")
    ]
    dataset = batch_payload.get("dataset") or {}
    runtime = batch_payload.get("runtime") or {}
    return {
        "cell_id": cell["cell_id"],
        "name": cell.get("name") or cell["cell_id"],
        "status": status,
        "agent_name": cell.get("agent_name", ""),
        "feature_set_name": cell.get("feature_set_name", ""),
        "runner": cell.get("runner", ""),
        "repetition": cell.get("repetition", 1),
        "dimensions": cell.get("dimensions") or {},
        "env": cell.get("env") or {},
        "env_unset": cell.get("env_unset") or [],
        "harness": cell.get("harness") or {},
        "returncode": returncode,
        "process_error": process_error,
        "cell_dir": str(cell_dir),
        "batch_id": batch_payload.get("batch_id", ""),
        "batch_dir": batch_payload.get("batch_dir", ""),
        "predictions_path": batch_payload.get("predictions_path", ""),
        "dataset": dataset,
        "runtime": runtime,
        "metrics": metrics,
        "tasks": instance_rows,
        "failed_tasks": failed_instances,
    }


def collect_run_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {key: 0 for key in SUM_METRIC_KEYS}
    ratios: dict[str, list[float]] = {key: [] for key in RATIO_METRIC_KEYS}
    models: set[str] = set()
    tools: set[str] = set()
    missing = 0
    for row in rows:
        metrics = row.get("metrics") or {}
        if not isinstance(metrics, dict) or not metrics:
            missing += 1
            continue
        for key in SUM_METRIC_KEYS:
            totals[key] += numeric(metrics.get(key))
        for key in RATIO_METRIC_KEYS:
            if metrics.get(key) not in (None, ""):
                ratios[key].append(numeric(metrics.get(key)))
        models.update(str(item) for item in metrics.get("models") or [] if item)
        tools.update(str(item) for item in metrics.get("tools") or [] if item)
    result: dict[str, Any] = {key: normalize_number(value) for key, value in totals.items()}
    for key, values in ratios.items():
        result[key] = normalize_number(sum(values) / len(values)) if values else 0
    result["models"] = sorted(models)
    result["tools"] = sorted(tools)
    result["missing_metrics_files"] = missing
    return result


def collect_instance_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_rows = []
    for row in rows:
        metrics = row.get("metrics") or {}
        status = str(row.get("status") or "")
        task_rows.append({
            "id": row.get("instance_id", ""),
            "repo": row.get("repo", ""),
            "base_commit": row.get("base_commit", ""),
            "status": status,
            "passed": status == "pass",
            "error": row.get("error", ""),
            "duration_ms": row.get("duration_ms", 0),
            "patch_bytes": row.get("patch_bytes", 0),
            "tool_calls": metrics.get("tool_calls", 0),
            "model_calls": metrics.get("model_calls", 0),
            "input_tokens": metrics.get("input_tokens", 0),
            "output_tokens": metrics.get("output_tokens", 0),
            "total_tokens": metrics.get("total_tokens", 0),
            "tool_failures": metrics.get("tool_failures", 0),
            "tool_denials": metrics.get("tool_denials", 0),
            "workspace": row.get("workspace", ""),
            "eval_dir": row.get("eval_dir", ""),
            "run_dir": row.get("run_dir", ""),
            "predictions_path": row.get("predictions_path", ""),
        })
    return task_rows


def summarize_matrix(cells: list[dict[str, Any]]) -> dict[str, Any]:
    completed = [cell for cell in cells if cell.get("status") in {"pass", "fail"}]
    passed = [cell for cell in cells if cell.get("status") == "pass"]
    failed = [cell for cell in cells if cell.get("status") == "fail"]
    errors = [cell for cell in cells if cell.get("status") == "error"]
    planned = [cell for cell in cells if cell.get("status") == "planned"]
    totals = {key: 0 for key in SUM_METRIC_KEYS}
    wall_duration_ms = 0.0
    for cell in cells:
        metrics = cell.get("metrics") or {}
        wall_duration_ms += numeric(metrics.get("wall_duration_ms"))
        for key in SUM_METRIC_KEYS:
            totals[key] += numeric(metrics.get(key))
    return {
        "cells": len(cells),
        "completed_cells": len(completed),
        "passed_cells": len(passed),
        "failed_cells": len(failed),
        "error_cells": len(errors),
        "planned_cells": len(planned),
        "cell_pass_rate": divide(len(passed), len(completed)),
        "wall_duration_ms": normalize_number(wall_duration_ms),
        **{f"sum_{key}": normalize_number(value) for key, value in totals.items()},
    }


def write_reports(matrix_root: Path, payload: dict[str, Any], *, metrics: list[str]) -> None:
    payload["summary"] = summarize_matrix(payload.get("cells") or [])
    write_json(matrix_root / "report.json", payload)
    write_csv(matrix_root / "rows.csv", payload.get("cells") or [], metrics=metrics)
    write_task_csv(matrix_root / "task_rows.csv", payload.get("cells") or [])
    write_text(matrix_root / "report.md", render_markdown(payload, metrics=metrics))


def write_csv(path: Path, cells: list[dict[str, Any]], *, metrics: list[str]) -> None:
    columns = [
        "cell_id",
        "status",
        "agent_name",
        "feature_set_name",
        "repetition",
        "dimensions_json",
        "env_json",
        "harness_json",
        "dataset_name",
        "split",
        "selected_count",
        *metrics,
        "batch_dir",
        "predictions_path",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for cell in cells:
            dataset = cell.get("dataset") or {}
            row = {
                "cell_id": cell.get("cell_id", ""),
                "status": cell.get("status", ""),
                "agent_name": cell.get("agent_name", ""),
                "feature_set_name": cell.get("feature_set_name", ""),
                "repetition": cell.get("repetition", ""),
                "dimensions_json": json.dumps(cell.get("dimensions") or {}, sort_keys=True, ensure_ascii=False),
                "env_json": json.dumps(cell.get("env") or {}, sort_keys=True, ensure_ascii=False),
                "harness_json": json.dumps(cell.get("harness") or {}, sort_keys=True, ensure_ascii=False),
                "dataset_name": dataset.get("resolved_name") or dataset.get("name", ""),
                "split": dataset.get("split", ""),
                "selected_count": dataset.get("selected_count", ""),
                "batch_dir": cell.get("batch_dir", ""),
                "predictions_path": cell.get("predictions_path", ""),
            }
            cell_metrics = cell.get("metrics") or {}
            for metric in metrics:
                row[metric] = cell_metrics.get(metric, "")
            writer.writerow(row)


def write_task_csv(path: Path, cells: list[dict[str, Any]]) -> None:
    columns = [
        "cell_id",
        "agent_name",
        "feature_set_name",
        "instance_id",
        "repo",
        "status",
        "passed",
        "patch_bytes",
        "duration_ms",
        "tool_calls",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tool_failures",
        "tool_denials",
        "error",
        "run_dir",
        "workspace",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for cell in cells:
            for task in cell.get("tasks") or []:
                writer.writerow({
                    "cell_id": cell.get("cell_id", ""),
                    "agent_name": cell.get("agent_name", ""),
                    "feature_set_name": cell.get("feature_set_name", ""),
                    "instance_id": task.get("id", ""),
                    "repo": task.get("repo", ""),
                    "status": task.get("status", ""),
                    "passed": task.get("passed", False),
                    "patch_bytes": task.get("patch_bytes", 0),
                    "duration_ms": task.get("duration_ms", 0),
                    "tool_calls": task.get("tool_calls", 0),
                    "model_calls": task.get("model_calls", 0),
                    "input_tokens": task.get("input_tokens", 0),
                    "output_tokens": task.get("output_tokens", 0),
                    "total_tokens": task.get("total_tokens", 0),
                    "tool_failures": task.get("tool_failures", 0),
                    "tool_denials": task.get("tool_denials", 0),
                    "error": task.get("error", ""),
                    "run_dir": task.get("run_dir", ""),
                    "workspace": task.get("workspace", ""),
                })


def print_instance_results(cell: dict[str, Any]) -> None:
    for task in cell.get("tasks") or []:
        marker = "PASS" if task.get("passed") else str(task.get("status", "FAIL")).upper()
        reason_text = f" error={task.get('error')}" if task.get("error") else ""
        print(
            "      "
            f"{marker} {task.get('id', '')} "
            f"repo={task.get('repo', '')} "
            f"patch={task.get('patch_bytes', 0)}B "
            f"tools={task.get('tool_calls', 0)} "
            f"tokens={task.get('total_tokens', 0)} "
            f"time={float(numeric(task.get('duration_ms'))):.0f}ms"
            f"{reason_text}",
            flush=True,
        )


def render_markdown(payload: dict[str, Any], *, metrics: list[str]) -> str:
    summary = payload.get("summary") or {}
    cells = payload.get("cells") or []
    lines = [
        "# SWE-bench Verified Matrix Eval",
        "",
        "## Summary",
        "",
        f"- Matrix ID: `{payload.get('matrix_id', '')}`",
        f"- Captured: `{payload.get('captured_at', '')}`",
        f"- Cells: {summary.get('cells', 0)}",
        f"- Completed: {summary.get('completed_cells', 0)}",
        f"- Passed cells: {summary.get('passed_cells', 0)}",
        f"- Failed cells: {summary.get('failed_cells', 0)}",
        f"- Error cells: {summary.get('error_cells', 0)}",
        f"- Cell pass rate: {format_value(summary.get('cell_pass_rate'), 'pass_rate')}",
        f"- Total tokens: {summary.get('sum_total_tokens', 0)}",
        f"- Total tool calls: {summary.get('sum_tool_calls', 0)}",
        f"- Wall duration: {format_duration(summary.get('wall_duration_ms', 0))}",
        f"- Matrix root: `{payload.get('matrix_root', '')}`",
        "",
        "## Cells",
        "",
        cell_table(cells, metrics=metrics),
        "",
    ]
    if has_cross_product(cells):
        lines.extend(["## Metric Matrix", ""])
        for metric in metrics:
            lines.extend([
                f"### {metric}",
                "",
                metric_pivot_table(cells, metric),
                "",
            ])

    failed = [cell for cell in cells if cell.get("status") in {"fail", "error"}]
    if failed:
        lines.extend(["## Failures", ""])
        for cell in failed:
            lines.extend([
                f"### {cell.get('cell_id', '')}",
                "",
                f"- Status: `{cell.get('status', '')}`",
                f"- Batch dir: `{cell.get('batch_dir', '')}`",
            ])
            if cell.get("process_error"):
                lines.append(f"- Process error: `{cell.get('process_error')}`")
            if cell.get("returncode"):
                lines.append(f"- Return code: `{cell.get('returncode')}`")
            for task in cell.get("failed_tasks") or []:
                reason = task.get("error") or task.get("status") or "failed"
                lines.append(f"- `{task.get('id', '')}`: {reason}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cell_table(cells: list[dict[str, Any]], *, metrics: list[str]) -> str:
    columns = [
        "cell",
        "status",
        "agent",
        "features",
        "dimensions",
        "instances",
        *metrics,
    ]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for cell in cells:
        cell_metrics = cell.get("metrics") or {}
        dataset = cell.get("dataset") or {}
        row = [
            f"`{cell.get('cell_id', '')}`",
            str(cell.get("status", "")),
            str(cell.get("agent_name", "")),
            str(cell.get("feature_set_name", "")),
            dimension_text(cell.get("dimensions") or {}),
            str(dataset.get("selected_count") or cell_metrics.get("total") or ""),
        ]
        row.extend(format_value(cell_metrics.get(metric), metric) for metric in metrics)
        lines.append("| " + " | ".join(escape_cell(value) for value in row) + " |")
    return "\n".join(lines)


def metric_pivot_table(cells: list[dict[str, Any]], metric: str) -> str:
    agents = sorted({str(cell.get("agent_name") or "") for cell in cells})
    features = sorted({str(cell.get("feature_set_name") or "") for cell in cells})
    if not agents or not features:
        return "_No agent/feature-set matrix available._"
    lines = [
        "| agent | " + " | ".join(features) + " |",
        "| --- | " + " | ".join("---:" for _ in features) + " |",
    ]
    for agent in agents:
        row = [agent]
        for feature in features:
            matches = [
                cell for cell in cells
                if cell.get("agent_name") == agent and cell.get("feature_set_name") == feature
            ]
            if not matches:
                row.append("")
                continue
            values = [numeric((cell.get("metrics") or {}).get(metric)) for cell in matches]
            value = sum(values) / len(values) if values else 0
            row.append(format_value(value, metric))
        lines.append("| " + " | ".join(escape_cell(value) for value in row) + " |")
    return "\n".join(lines)


def has_cross_product(cells: list[dict[str, Any]]) -> bool:
    return (
        len({cell.get("agent_name") for cell in cells}) >= 1
        and len({cell.get("feature_set_name") for cell in cells}) > 1
    )


def parse_ids(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    parsed: list[str] = []
    for item in items:
        parsed.extend(part.strip() for part in str(item).split(",") if part.strip())
    return parsed


def truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
