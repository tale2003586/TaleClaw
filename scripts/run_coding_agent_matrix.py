from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


ROOT = Path(__file__).resolve().parent.parent
RUN_SYSTEM_EVALS = ROOT / "scripts" / "run_system_evals.py"

DEFAULT_BENCHMARK_PATH = "benchmarks/coding_tasks.json"
DEFAULT_OUTPUT_ROOT = ".evals/coding_agent_matrix"

DEFAULT_METRICS = [
    "pass_rate",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "wall_duration_ms",
    "avg_reasoning_steps",
    "avg_tool_calls_per_task",
    "tool_calls",
    "model_calls",
    "tool_failures",
    "tool_denials",
    "context_build_compressed_count",
]

SUM_METRIC_KEYS = [
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "model_calls",
    "model_failures",
    "tool_calls",
    "tool_failures",
    "tool_denials",
    "total_model_duration_ms",
    "total_tool_duration_ms",
    "run_duration_ms",
    "model_retry_count",
    "model_route_attempts",
    "sanitized_messages",
    "duplicate_tool_call_count",
    "truncated_tool_output_count",
    "subagent_incomplete_count",
    "subagent_fanout_count",
    "context_builds",
    "context_build_compressed_count",
    "total_context_build_duration_ms",
    "context_compression_before_chars",
    "context_compression_after_chars",
    "context_compression_saved_chars",
]

RATIO_METRIC_KEYS = [
    "duplicate_tool_call_ratio",
    "context_compression_ratio",
    "context_compression_savings_ratio",
    "avg_context_build_duration_ms",
    "max_context_build_duration_ms",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run coding-agent benchmark variants as a configurable matrix.",
    )
    parser.add_argument(
        "--config",
        default="",
        help="Matrix config JSON. If omitted, a small built-in scripted matrix is used.",
    )
    parser.add_argument(
        "--output-root",
        default=DEFAULT_OUTPUT_ROOT,
        help="Directory where matrix artifacts are written.",
    )
    parser.add_argument(
        "--benchmark-path",
        default="",
        help="Override benchmark task JSON path from the config.",
    )
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help="Run only these task ids. May be repeated or comma-separated.",
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
        help="Write the expanded matrix plan and report without running benchmarks.",
    )
    parser.add_argument(
        "--fail-fast",
        action="store_true",
        help="Stop after the first cell process error.",
    )
    parser.add_argument(
        "--fail-on-benchmark-fail",
        action="store_true",
        help="Exit non-zero when any completed benchmark cell has failed tasks.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress per-cell progress output.",
    )
    parser.add_argument(
        "--write-template",
        default="",
        help="Write an editable example matrix config JSON to this path and exit.",
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
    if args.benchmark_path:
        config["benchmark_path"] = args.benchmark_path
    cli_task_ids = parse_task_ids(args.task_id)
    if cli_task_ids:
        config["task_ids"] = cli_task_ids

    matrix_id = "matrix_" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    matrix_root = (Path(args.output_root) / matrix_id).resolve()
    matrix_root.mkdir(parents=True, exist_ok=True)

    cells = expand_cells(config)
    if args.limit_cells > 0:
        cells = cells[: args.limit_cells]
    payload = {
        "schema_version": 1,
        "matrix_id": matrix_id,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "matrix_root": str(matrix_root),
        "config": config,
        "dry_run": bool(args.dry_run),
        "cells": [],
        "summary": {},
    }
    (matrix_root / "expanded_plan.json").write_text(
        json.dumps({"cells": cells, "config": config}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    for index, cell in enumerate(cells, start=1):
        if not args.quiet:
            print(
                f"[{index:02d}/{len(cells):02d}] {cell['cell_id']} "
                f"agent={cell['agent_name']} feature={cell['feature_set_name']} "
                f"runner={cell['runner']}",
                flush=True,
            )
        result = (
            dry_run_cell(cell, matrix_root=matrix_root)
            if args.dry_run
            else run_cell(cell, matrix_root=matrix_root, quiet=args.quiet)
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
            f"[matrix] DONE cells={summary['cells']} pass={summary['passed_cells']} "
            f"error={summary['error_cells']} artifacts={matrix_root}",
            flush=True,
        )
    print(f"wrote matrix report: {matrix_root / 'report.md'}")

    if payload["summary"].get("error_cells", 0):
        return 1
    if args.fail_on_benchmark_fail and payload["summary"].get("failed_cells", 0):
        return 1
    return 0


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise SystemExit("Matrix config must be a JSON object.")
    schema_version = int(payload.get("schema_version", 1))
    if schema_version != 1:
        raise SystemExit(f"Unsupported matrix config schema_version: {schema_version}")
    return payload


def default_config() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "name": "coding-agent-default-scripted-matrix",
        "benchmark_path": DEFAULT_BENCHMARK_PATH,
        "repetitions": 1,
        "harness_defaults": {
            "keep_workspace": False,
        },
        "env": {
            "LLM_HEALTHCHECK_ON_STARTUP": "0",
        },
        "agents": [
            {
                "name": "scripted",
                "runner": "scripted",
                "dimensions": {"agent": "scripted"},
            }
        ],
        "feature_sets": [
            {
                "name": "budget-on",
                "env": {
                    "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                },
                "harness": {"no_step_budget": False},
                "dimensions": {
                    "context_budget": "on",
                    "reasoning_step_budget": "on",
                },
            },
            {
                "name": "context-budget-off",
                "env": {
                    "CONTEXT_ENABLE_SECTION_BUDGET": "0",
                },
                "harness": {"no_step_budget": False},
                "dimensions": {
                    "context_budget": "off",
                    "reasoning_step_budget": "on",
                },
            },
            {
                "name": "step-budget-off",
                "env": {
                    "CONTEXT_ENABLE_SECTION_BUDGET": "1",
                },
                "harness": {"no_step_budget": True},
                "dimensions": {
                    "context_budget": "on",
                    "reasoning_step_budget": "off",
                },
            },
        ],
        "metrics": DEFAULT_METRICS,
    }


def template_config() -> dict[str, Any]:
    config = default_config()
    config["name"] = "coding-agent-matrix-template"
    config["notes"] = [
        "Edit agents and feature_sets freely. Enabled entries are crossed as agents x feature_sets x task_ids x repetitions.",
        "Use env for runtime feature flags. Each cell runs in a fresh Python subprocess so import-time config constants see these values.",
        "Set enabled=false on expensive real-model variants until you are ready to run them.",
    ]
    config["task_ids"] = ["coding-git-diff-008"]
    config["agents"].append({
        "name": "real-default",
        "enabled": False,
        "runner": "real",
        "env": {
            "LLM_ROUTE_CODING": "openai_relay,deepseek,mimo",
        },
        "dimensions": {
            "agent": "real",
            "model_route": "default",
        },
    })
    return config


def expand_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    benchmark_path = str(config.get("benchmark_path") or DEFAULT_BENCHMARK_PATH)
    repetitions = max(1, int(config.get("repetitions") or 1))
    task_ids = parse_task_ids(config.get("task_ids") or [])
    if not task_ids:
        task_ids = [""]

    if config.get("variants"):
        base_cells = expand_variant_cells(config)
    else:
        base_cells = expand_agent_feature_cells(config)

    cells = []
    seen_ids: set[str] = set()
    for base in base_cells:
        for task_id in task_ids:
            for repetition in range(1, repetitions + 1):
                cell = deepcopy(base)
                cell["benchmark_path"] = benchmark_path
                cell["task_id"] = task_id
                cell["repetition"] = repetition
                if task_id:
                    cell["dimensions"]["task_id"] = task_id
                if repetitions > 1:
                    cell["dimensions"]["repetition"] = str(repetition)
                stem = cell["cell_id"]
                if task_id:
                    stem += f"__{slug(task_id)}"
                if repetitions > 1:
                    stem += f"__r{repetition}"
                cell["cell_id"] = unique_id(stem, seen_ids)
                cells.append(cell)
    if not cells:
        raise SystemExit("Matrix config expanded to zero cells.")
    return cells


def expand_variant_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    cells = []
    global_env = dict(config.get("env") or {})
    harness_defaults = dict(config.get("harness_defaults") or {})
    for variant in config.get("variants") or []:
        if not is_enabled(variant):
            continue
        name = str(variant.get("name") or "variant").strip()
        env = merge_env(global_env, variant.get("env") or {})
        env_unset = list(config.get("env_unset") or []) + list(variant.get("env_unset") or [])
        harness = merge_dicts(harness_defaults, variant.get("harness") or {})
        dimensions = {
            "variant": name,
            **dict(variant.get("dimensions") or {}),
        }
        runner = str(variant.get("runner") or harness.pop("runner", "") or "scripted")
        cells.append({
            "cell_id": slug(name),
            "name": name,
            "agent_name": str(variant.get("agent_name") or name),
            "feature_set_name": str(variant.get("feature_set_name") or "custom"),
            "runner": runner,
            "env": env,
            "env_unset": env_unset,
            "harness": harness,
            "extra_args": list(variant.get("extra_args") or []),
            "timeout_seconds": int(variant.get("timeout_seconds") or config.get("timeout_seconds") or 0),
            "dimensions": dimensions,
        })
    return cells


def expand_agent_feature_cells(config: dict[str, Any]) -> list[dict[str, Any]]:
    agents = [item for item in config.get("agents") or [] if is_enabled(item)]
    feature_sets = [item for item in config.get("feature_sets") or [] if is_enabled(item)]
    if not agents:
        raise SystemExit("Matrix config must define at least one enabled agent.")
    if not feature_sets:
        feature_sets = [{"name": "default"}]

    global_env = dict(config.get("env") or {})
    harness_defaults = dict(config.get("harness_defaults") or {})
    cells = []
    for agent in agents:
        agent_name = str(agent.get("name") or "agent").strip()
        for feature_set in feature_sets:
            feature_name = str(feature_set.get("name") or "feature").strip()
            env = merge_env(global_env, agent.get("env") or {}, feature_set.get("env") or {})
            env_unset = (
                list(config.get("env_unset") or [])
                + list(agent.get("env_unset") or [])
                + list(feature_set.get("env_unset") or [])
            )
            harness = merge_dicts(
                harness_defaults,
                agent.get("harness") or {},
                feature_set.get("harness") or {},
            )
            runner = str(
                feature_set.get("runner")
                or agent.get("runner")
                or harness.pop("runner", "")
                or "scripted"
            )
            dimensions = {
                "agent": agent_name,
                "feature_set": feature_name,
                **dict(agent.get("dimensions") or {}),
                **dict(feature_set.get("dimensions") or {}),
            }
            cells.append({
                "cell_id": slug(f"{agent_name}__{feature_name}"),
                "name": f"{agent_name} / {feature_name}",
                "agent_name": agent_name,
                "feature_set_name": feature_name,
                "runner": runner,
                "env": env,
                "env_unset": env_unset,
                "harness": harness,
                "extra_args": list(agent.get("extra_args") or []) + list(feature_set.get("extra_args") or []),
                "timeout_seconds": int(
                    feature_set.get("timeout_seconds")
                    or agent.get("timeout_seconds")
                    or config.get("timeout_seconds")
                    or 0
                ),
                "dimensions": dimensions,
            })
    return cells


def run_cell(cell: dict[str, Any], *, matrix_root: Path, quiet: bool) -> dict[str, Any]:
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

    cmd = [
        sys.executable,
        str(RUN_SYSTEM_EVALS),
        "--tasks",
        str(cell["benchmark_path"]),
        "--output-root",
        str(eval_root),
        "--workspace-root",
        str(workspace_root),
        "--runner",
        str(cell.get("runner") or "scripted"),
        "--quiet",
    ]
    harness = cell.get("harness") or {}
    if cell.get("task_id"):
        cmd.extend(["--task-id", str(cell["task_id"])])
    if bool(harness.get("keep_workspace")):
        cmd.append("--keep-workspace")
    if bool(harness.get("no_step_budget")):
        cmd.append("--no-step-budget")
    if harness.get("max_reasoning_steps") not in (None, ""):
        cmd.extend(["--max-reasoning-steps", str(int(harness["max_reasoning_steps"]))])
    cmd.extend(str(item) for item in cell.get("extra_args") or [])

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
    (cell_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
    (cell_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    (cell_dir / "command.json").write_text(
        json.dumps(
            {
                "cmd": cmd,
                "env_overrides": cell.get("env") or {},
                "env_unset": cell.get("env_unset") or [],
                "returncode": returncode,
                "process_error": process_error,
                "wall_duration_ms": wall_duration_ms,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    eval_payload = load_single_eval_payload(eval_root)
    result = build_cell_result(
        cell,
        cell_dir=cell_dir,
        eval_payload=eval_payload,
        returncode=returncode,
        process_error=process_error,
        wall_duration_ms=wall_duration_ms,
    )
    (cell_dir / "cell.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
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
        print_task_results(result)
    return result


def dry_run_cell(cell: dict[str, Any], *, matrix_root: Path) -> dict[str, Any]:
    cell_dir = matrix_root / "cells" / cell["cell_id"]
    cell_dir.mkdir(parents=True, exist_ok=True)
    result = build_cell_result(
        cell,
        cell_dir=cell_dir,
        eval_payload={},
        returncode=0,
        process_error="",
        wall_duration_ms=0,
        dry_run=True,
    )
    (cell_dir / "cell.json").write_text(
        json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def load_single_eval_payload(eval_root: Path) -> dict[str, Any]:
    eval_dirs = sorted(path for path in eval_root.glob("eval_*") if path.is_dir())
    if not eval_dirs:
        return {}
    latest = eval_dirs[-1]
    summary_path = latest / "summary.json"
    if not summary_path.exists():
        return {"eval_dir": str(latest)}
    payload = json.loads(summary_path.read_text(encoding="utf-8"))
    payload["eval_dir"] = str(latest)
    return payload


def build_cell_result(
    cell: dict[str, Any],
    *,
    cell_dir: Path,
    eval_payload: dict[str, Any],
    returncode: int,
    process_error: str,
    wall_duration_ms: float,
    dry_run: bool = False,
) -> dict[str, Any]:
    rows = list(eval_payload.get("rows") or [])
    eval_summary = dict(eval_payload.get("summary") or {})
    run_metrics = collect_run_metrics(rows)
    task_count = int(eval_summary.get("total_tasks") or len(rows) or 0)
    metrics = {
        **eval_summary,
        **run_metrics,
        "wall_duration_ms": wall_duration_ms,
        "avg_total_tokens_per_task": divide(run_metrics.get("total_tokens", 0), task_count),
        "avg_input_tokens_per_task": divide(run_metrics.get("input_tokens", 0), task_count),
        "avg_output_tokens_per_task": divide(run_metrics.get("output_tokens", 0), task_count),
        "avg_tool_calls_per_task": divide(run_metrics.get("tool_calls", 0), task_count),
        "avg_model_calls_per_task": divide(run_metrics.get("model_calls", 0), task_count),
        "avg_wall_duration_ms_per_task": divide(wall_duration_ms, task_count),
    }
    if dry_run:
        status = "planned"
    elif returncode != 0 or process_error or not eval_payload:
        status = "error"
    elif int(eval_summary.get("failed") or 0) > 0:
        status = "fail"
    else:
        status = "pass"

    task_rows = collect_task_rows(rows)
    failed_tasks = [
        {
            "id": task.get("id", ""),
            "category": task.get("category", ""),
            "failure_category": task.get("failure_category", ""),
            "failure_reason": task.get("failure_reason", ""),
            "run_dir": task.get("run_dir", ""),
        }
        for task in task_rows
        if not task.get("passed")
    ]
    return {
        "cell_id": cell["cell_id"],
        "name": cell.get("name") or cell["cell_id"],
        "status": status,
        "agent_name": cell.get("agent_name", ""),
        "feature_set_name": cell.get("feature_set_name", ""),
        "runner": cell.get("runner", ""),
        "benchmark_path": cell.get("benchmark_path", ""),
        "task_id": cell.get("task_id", ""),
        "repetition": cell.get("repetition", 1),
        "dimensions": cell.get("dimensions") or {},
        "env": cell.get("env") or {},
        "env_unset": cell.get("env_unset") or [],
        "harness": cell.get("harness") or {},
        "returncode": returncode,
        "process_error": process_error,
        "cell_dir": str(cell_dir),
        "eval_id": eval_payload.get("eval_id", ""),
        "eval_dir": eval_payload.get("eval_dir", ""),
        "metrics": metrics,
        "tasks": task_rows,
        "failed_tasks": failed_tasks,
    }


def collect_run_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals: dict[str, float] = {key: 0 for key in SUM_METRIC_KEYS}
    ratios: dict[str, list[float]] = {key: [] for key in RATIO_METRIC_KEYS}
    models: set[str] = set()
    tools: set[str] = set()
    missing = 0
    for row in rows:
        metrics = read_row_metrics(row)
        if not metrics:
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


def collect_task_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    task_rows = []
    for row in rows:
        metrics = read_row_metrics(row)
        task_rows.append({
            "id": row.get("id", ""),
            "category": row.get("category", ""),
            "status": row.get("status", ""),
            "passed": bool(row.get("passed")),
            "failure_category": row.get("failure_category", ""),
            "failure_reason": row.get("failure_reason", ""),
            "duration_ms": row.get("duration_ms", 0),
            "reasoning_steps": row.get("reasoning_steps", 0),
            "tool_calls": row.get("tool_calls", 0),
            "metric_tool_calls": metrics.get("tool_calls", 0),
            "model_calls": metrics.get("model_calls", 0),
            "input_tokens": metrics.get("input_tokens", 0),
            "output_tokens": metrics.get("output_tokens", 0),
            "total_tokens": metrics.get("total_tokens", 0),
            "tool_failures": metrics.get("tool_failures", 0),
            "tool_denials": metrics.get("tool_denials", 0),
            "run_dir": row.get("run_dir", ""),
        })
    return task_rows


def read_row_metrics(row: dict[str, Any]) -> dict[str, Any]:
    run_dir = Path(str(row.get("run_dir") or ""))
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        return {}
    try:
        metrics = json.loads(metrics_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return metrics if isinstance(metrics, dict) else {}


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
    (matrix_root / "report.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    write_csv(matrix_root / "rows.csv", payload.get("cells") or [], metrics=metrics)
    write_task_csv(matrix_root / "task_rows.csv", payload.get("cells") or [])
    (matrix_root / "report.md").write_text(
        render_markdown(payload, metrics=metrics),
        encoding="utf-8",
    )


def write_csv(path: Path, cells: list[dict[str, Any]], *, metrics: list[str]) -> None:
    columns = [
        "cell_id",
        "status",
        "agent_name",
        "feature_set_name",
        "runner",
        "task_id",
        "repetition",
        "dimensions_json",
        "env_json",
        "harness_json",
        *metrics,
        "eval_dir",
    ]
    with path.open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        for cell in cells:
            row = {
                "cell_id": cell.get("cell_id", ""),
                "status": cell.get("status", ""),
                "agent_name": cell.get("agent_name", ""),
                "feature_set_name": cell.get("feature_set_name", ""),
                "runner": cell.get("runner", ""),
                "task_id": cell.get("task_id", ""),
                "repetition": cell.get("repetition", ""),
                "dimensions_json": json.dumps(cell.get("dimensions") or {}, sort_keys=True, ensure_ascii=False),
                "env_json": json.dumps(cell.get("env") or {}, sort_keys=True, ensure_ascii=False),
                "harness_json": json.dumps(cell.get("harness") or {}, sort_keys=True, ensure_ascii=False),
                "eval_dir": cell.get("eval_dir", ""),
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
        "runner",
        "task_id",
        "task_status",
        "passed",
        "category",
        "failure_category",
        "failure_reason",
        "duration_ms",
        "reasoning_steps",
        "tool_calls",
        "metric_tool_calls",
        "model_calls",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "tool_failures",
        "tool_denials",
        "run_dir",
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
                    "runner": cell.get("runner", ""),
                    "task_id": task.get("id", ""),
                    "task_status": task.get("status", ""),
                    "passed": task.get("passed", False),
                    "category": task.get("category", ""),
                    "failure_category": task.get("failure_category", ""),
                    "failure_reason": task.get("failure_reason", ""),
                    "duration_ms": task.get("duration_ms", 0),
                    "reasoning_steps": task.get("reasoning_steps", 0),
                    "tool_calls": task.get("tool_calls", 0),
                    "metric_tool_calls": task.get("metric_tool_calls", 0),
                    "model_calls": task.get("model_calls", 0),
                    "input_tokens": task.get("input_tokens", 0),
                    "output_tokens": task.get("output_tokens", 0),
                    "total_tokens": task.get("total_tokens", 0),
                    "tool_failures": task.get("tool_failures", 0),
                    "tool_denials": task.get("tool_denials", 0),
                    "run_dir": task.get("run_dir", ""),
                })


def print_task_results(cell: dict[str, Any]) -> None:
    tasks = cell.get("tasks") or []
    if not tasks:
        return
    for task in tasks:
        marker = "PASS" if task.get("passed") else "FAIL"
        reason = task.get("failure_reason") or task.get("failure_category") or ""
        reason_text = f" reason={reason}" if reason else ""
        print(
            "      "
            f"{marker} {task.get('id', '')} "
            f"category={task.get('category', '')} "
            f"steps={task.get('reasoning_steps', 0)} "
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
        "# Coding Agent Matrix Eval",
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
                f"- Eval dir: `{cell.get('eval_dir', '')}`",
            ])
            if cell.get("process_error"):
                lines.append(f"- Process error: `{cell.get('process_error')}`")
            if cell.get("returncode"):
                lines.append(f"- Return code: `{cell.get('returncode')}`")
            for task in cell.get("failed_tasks") or []:
                reason = task.get("failure_reason") or task.get("failure_category") or "failed"
                lines.append(f"- `{task.get('id', '')}`: {reason}")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def cell_table(cells: list[dict[str, Any]], *, metrics: list[str]) -> str:
    selected_metrics = metrics[:]
    columns = [
        "cell",
        "status",
        "agent",
        "features",
        "task",
        "dimensions",
        *selected_metrics,
    ]
    lines = [
        "| " + " | ".join(columns) + " |",
        "| " + " | ".join("---" for _ in columns) + " |",
    ]
    for cell in cells:
        cell_metrics = cell.get("metrics") or {}
        row = [
            f"`{cell.get('cell_id', '')}`",
            str(cell.get("status", "")),
            str(cell.get("agent_name", "")),
            str(cell.get("feature_set_name", "")),
            str(cell.get("task_id") or "all"),
            dimension_text(cell.get("dimensions") or {}),
        ]
        row.extend(format_value(cell_metrics.get(metric), metric) for metric in selected_metrics)
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


def parse_task_ids(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        items = [value]
    else:
        items = list(value)
    parsed = []
    for item in items:
        parsed.extend(part.strip() for part in str(item).split(",") if part.strip())
    return parsed


def is_enabled(item: dict[str, Any]) -> bool:
    return bool(item.get("enabled", True))


def merge_env(*items: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        for key, value in dict(item).items():
            merged[str(key)] = value
    return merged


def merge_dicts(*items: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for item in items:
        for key, value in dict(item).items():
            merged[str(key)] = value
    return merged


def env_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    return str(value)


def unique_id(value: str, seen: set[str]) -> str:
    base = slug(value)
    candidate = base
    index = 2
    while candidate in seen:
        candidate = f"{base}-{index}"
        index += 1
    seen.add(candidate)
    return candidate


def slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value).strip().lower())
    text = re.sub(r"-+", "-", text).strip("-._")
    return text or "cell"


def numeric(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def divide(numerator: Any, denominator: Any) -> float:
    denom = numeric(denominator)
    if denom == 0:
        return 0.0
    return normalize_number(numeric(numerator) / denom)


def normalize_number(value: float) -> int | float:
    if float(value).is_integer():
        return int(value)
    return round(float(value), 6)


def format_value(value: Any, metric: str) -> str:
    number = numeric(value)
    if metric.endswith("rate") or metric.endswith("ratio"):
        return f"{number:.2%}"
    if metric.endswith("_ms") or metric == "wall_duration_ms":
        return format_duration(number)
    if float(number).is_integer():
        return str(int(number))
    return f"{number:.3f}"


def format_duration(value: Any) -> str:
    ms = numeric(value)
    if ms >= 1000:
        return f"{ms / 1000:.2f}s"
    return f"{ms:.0f}ms"


def escape_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def dimension_text(dimensions: dict[str, Any]) -> str:
    if not dimensions:
        return ""
    return ", ".join(f"{key}={value}" for key, value in sorted(dimensions.items()))


if __name__ == "__main__":
    raise SystemExit(main())
