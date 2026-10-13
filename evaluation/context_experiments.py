"""Paired context-policy experiments using the production coding runner.

python -m evaluation.context_experiments --runner scripted --repeat 1
python -m evaluation.context_experiments --runner real --task-id coding-git-diff-008
"""
from __future__ import annotations

import argparse
import csv
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
import random
from statistics import mean
from types import SimpleNamespace
from uuid import uuid4

from evaluation.harness import CodingBenchmarkHarness, DEFAULT_BENCHMARK_PATH
from evaluation.provenance import evaluation_manifest
from evaluation.task_schema import load_benchmark
from runtime.context.policies import context_policy, POLICY_NAMES


def _effective_policies(row: dict) -> set[str]:
    resolution = (row.get("policy_observation") or {}).get("resolution") or {}
    values = [resolution.get("effective"), *[call.get("policy") for call in row.get("actual_model_calls", [])]]
    return {str(value) for value in values if value}


def _observed_models(row: dict) -> frozenset | None:
    calls = row.get("actual_model_calls") or []
    # Missing trace identity cannot establish a fair same-model comparison.
    if not calls or any(not call.get("model") or not call.get("provider") for call in calls):
        return None
    return frozenset((call["model"], call.get("profile"), call["provider"]) for call in calls)


def _model_scope_complete(row: dict) -> bool:
    attempts = (row.get("execution_budget") or {}).get("model_attempts")
    return (_observed_models(row) is not None and type(attempts) is int
            and attempts == len(row.get("actual_model_calls") or []))


def _intervention_status(row: dict) -> str:
    evidence = row.get("context_intervention") or {}
    status = evidence.get("status")
    if (evidence.get("schema_version") != 1 or status not in {"observed", "none"}
            or type(evidence.get("requested_calls")) is not int or evidence["requested_calls"] < 1
            or evidence.get("audited_calls") != evidence["requested_calls"]
            or evidence.get("reasons")):
        return "unknown"
    if (status == "observed" and evidence.get("intervention_observed") is not True
            or status == "none" and evidence.get("intervention_observed") is not False):
        return "unknown"
    return status


def _provenance_pair_reason(first: dict, second: dict, *, task_id: str) -> str | None:
    """Policy is the intended treatment; code, fixtures and execution limits are not."""
    manifests = []
    for cell in (first, second):
        initial, final = cell.get("manifest"), cell.get("manifest_after")
        if not isinstance(initial, dict) or not isinstance(final, dict):
            return "provenance_unknown"
        manifests.extend((initial, final))
    for manifest in manifests:
        sources = manifest.get("source_files")
        fixture = (manifest.get("fixtures") or {}).get(task_id) or {}
        data = manifest.get("dataset_file") or {}
        digests = (manifest.get("source_set_sha256"), manifest.get("fixtures_sha256"), data.get("sha256"))
        if (any(not isinstance(value, str) or len(value) != 64 for value in digests)
                or not isinstance(sources, dict) or not sources
                or any(item.get("status") != "available" for item in sources.values())
                or data.get("status") != "available" or fixture.get("status") != "available"
                or not any(item.get("status") == "available" for item in (fixture.get("files") or {}).values())
                or not isinstance(manifest.get("configuration"), dict)):
            return "provenance_unknown"
    if len({item["source_set_sha256"] for item in manifests}) != 1:
        return "source_changed"
    if len({item["fixtures_sha256"] for item in manifests}) != 1:
        return "fixture_changed"
    if len({item["dataset_file"]["sha256"] for item in manifests}) != 1:
        return "dataset_changed"
    configurations = [{key: value for key, value in item["configuration"].items()
                       if key not in {"context_policy", "policies"}} for item in manifests]
    if any(value != configurations[0] for value in configurations[1:]):
        return "execution_configuration_changed"
    return None


def _unknown_row(identifier: str, reason: str, *, error_type: str = "", unsupported: bool = False) -> dict:
    row = {"id": identifier, "verification": {"outcome": "inconclusive", "reason": reason},
           "execution_status": "not_run", "error_type": error_type}
    if unsupported:
        row["policy_observation"] = {"resolution": {"requested": "native", "effective": None, "unsupported": True}}
    return row


def summarize_cells(cells: list[dict]) -> dict:
    result = {}
    for name in dict.fromkeys(cell["policy"]["name"] for cell in cells):
        selected = [c for c in cells if c["policy"]["name"] == name]
        all_rows = [row for cell in selected for row in cell.get("rows", [])]
        planned = sum(len(c.get("expected_task_ids", c.get("rows", []))) for c in selected)
        fallback_rows = [r for r in all_rows if _effective_policies(r) and _effective_policies(r) != {name}]
        unsupported_rows = [r for r in all_rows if ((r.get("policy_observation") or {}).get("resolution") or {}).get("unsupported") or r.get("error_type") == "NativeContextUnsupported"]
        rows = [r for r in all_rows if r not in fallback_rows and r not in unsupported_rows]
        attributable = [r for r in rows if _effective_policies(r) == {name}]
        passed = sum((r.get("verification") or {}).get("outcome") == "pass" for r in attributable)
        failed = sum((r.get("verification") or {}).get("outcome") == "fail" for r in attributable)
        # Every attempted path incurs cost, including fallback and failed cases.
        budgets = [r.get("execution_budget") or {} for r in all_rows]
        complete = bool(all_rows) and len(all_rows) == planned and all(c.get("runner_mode") == "real" for c in selected) and all(
            b.get("usage_complete") is True and b.get("model_attempts", 0) > 0 for b in budgets)
        known = sum(b.get("known_tokens", 0) for b in budgets)
        durations = [r["duration_ms"] for r in all_rows if isinstance(r.get("duration_ms"), (int, float))]
        result[name] = {"planned_cells": len(selected), "planned_tasks": planned,
                        "error_cells": sum(c["status"] == "error" for c in selected),
                        "unsupported_cells": sum(c["status"] == "unsupported" for c in selected),
                        "tasks": len(rows), "recorded_attempts": len(all_rows),
                        "fallback_tasks": len(fallback_rows), "unsupported_tasks": len(unsupported_rows),
                        "policy_unobserved_tasks": sum(not _effective_policies(r) for r in rows),
                        "effective_policies": sorted(set().union(*(_effective_policies(r) for r in all_rows))) if all_rows else [],
                        "verified_pass": passed, "verified_fail": failed,
                        "execution_completed_tasks": sum(r.get("execution_status") == "completed" for r in attributable),
                        "verified_and_completed_tasks": sum((r.get("verification") or {}).get("outcome") == "pass" and r.get("execution_status") == "completed" for r in attributable),
                        "intervention_observed_tasks": sum(_intervention_status(r) == "observed" for r in attributable),
                        "intervention_none_tasks": sum(_intervention_status(r) == "none" for r in attributable),
                        "intervention_unknown_tasks": sum(_intervention_status(r) == "unknown" for r in attributable),
                        "hard_trim_tasks": sum((r.get("context_intervention") or {}).get("hard_trim_calls", 0) > 0 for r in attributable),
                        "unverified": len(rows) - passed - failed,
                        "planned_completion_rate": passed / planned if planned else None,
                        "pass_rate_all_attempts": passed / planned if planned and len(unsupported_rows) < planned else None,
                        "known_tokens": known, "total_tokens": known if complete else None,
                        "usage_complete": complete,
                        "model_attempts": sum(b.get("model_attempts", 0) for b in budgets),
                        "cached_input_tokens_known": sum((b.get("known_usage_totals") or {}).get("cached_input_tokens", 0) for b in budgets),
                        "cache_usage_complete": bool(budgets) and complete and all(
                            (b.get("usage_field_counts") or {}).get("cached_input_tokens", 0) == b.get("model_attempts") for b in budgets),
                        "mean_duration_ms": round(mean(durations), 3) if durations else None}
    baseline = {(c["repeat"], row["id"]): (c, row) for c in cells if c["policy"]["name"] == "current" for row in c.get("rows", [])}
    for name, summary in result.items():
        valid = []
        exclusions = {}
        for cell in cells:
            if cell["policy"]["name"] != name:
                continue
            for row in cell.get("rows", []):
                prior = baseline.get((cell["repeat"], row["id"]))
                if prior is None:
                    exclusions["baseline_missing"] = exclusions.get("baseline_missing", 0) + 1
                    continue
                prior_cell, first = prior
                identities = _observed_models(first)
                provenance_reason = _provenance_pair_reason(prior_cell, cell, task_id=row["id"])
                if (provenance_reason is None and cell["runner_mode"] == prior_cell["runner_mode"]
                        and identities is not None and identities == _observed_models(row)
                        and _model_scope_complete(first) and _model_scope_complete(row)
                        and _effective_policies(first) == {"current"} and _effective_policies(row) == {name}
                        and all((x.get("verification") or {}).get("outcome") in {"pass", "fail"} for x in (first, row))):
                    valid.append((first, row, cell["runner_mode"]))
                else:
                    reason = (provenance_reason or ("model_scope_unknown" if not _model_scope_complete(first) or not _model_scope_complete(row)
                              else "model_mismatch" if identities != _observed_models(row)
                              else "policy_or_outcome_mismatch"))
                    exclusions[reason] = exclusions.get(reason, 0) + 1
        intervened = [(a, b, runner_mode) for a, b, runner_mode in valid if name != "current"
                      and all(_intervention_status(r) != "unknown" for r in (a, b))
                      and any(_intervention_status(r) == "observed" for r in (a, b))]
        policy_only = [(a, b, runner_mode) for a, b, runner_mode in intervened
                       if not any((r.get("context_intervention") or {}).get("hard_trim_calls", 0) for r in (a, b))]
        cost_pairs = [(a, b) for a, b, runner_mode in policy_only if runner_mode == "real" and all(
            (r.get("verification") or {}).get("outcome") == "pass" and r.get("execution_status") == "completed"
            and (r.get("execution_budget") or {}).get("usage_complete") is True for r in (a, b))]
        summary["compression_comparison_status"] = "eligible_verified_cost_pairs" if cost_pairs else "not_established"
        summary["paired_vs_current"] = {"valid_pairs": len(valid), "excluded_pairs": exclusions,
            "intervention_pairs": len(intervened), "policy_only_intervention_pairs": len(policy_only),
            "verified_cost_pairs": len(cost_pairs),
            "unintervened_pairs": sum(all(_intervention_status(r) == "none" for r in (a, b)) for a, b, _ in valid),
            "intervention_unknown_pairs": sum(any(_intervention_status(r) == "unknown" for r in (a, b)) for a, b, _ in valid),
            "wins": sum(a["verification"]["outcome"] == "fail" and b["verification"]["outcome"] == "pass" for a,b,_ in valid),
            "losses": sum(a["verification"]["outcome"] == "pass" and b["verification"]["outcome"] == "fail" for a,b,_ in valid),
            "ties": sum(a["verification"]["outcome"] == b["verification"]["outcome"] for a,b,_ in valid)}
    return result


def _write_report(directory: Path, payload: dict) -> None:
    # Atomic checkpoints keep completed cells reviewable after an interrupted run.
    payload["summary"] = summarize_cells(payload["cells"])
    temporary = directory / "matrix.json.tmp"
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    temporary.replace(directory / "matrix.json")
    with (directory / "rows.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["policy", "repeat", "task", "verification", "execution", "known_tokens", "usage_complete", "duration_ms", "intervention", "hard_trim_calls"])
        writer.writeheader()
        for cell in payload["cells"]:
            for row in cell.get("rows", []):
                budget = row.get("execution_budget") or {}
                writer.writerow({"policy": cell["policy"]["name"], "repeat": cell["repeat"], "task": row["id"],
                    "verification": (row.get("verification") or {}).get("outcome", "inconclusive"),
                    "execution": row.get("execution_status", "unknown"), "known_tokens": budget.get("known_tokens"),
                    "usage_complete": budget.get("usage_complete") if cell["runner_mode"] == "real" else False,
                    "duration_ms": row.get("duration_ms"), "intervention": _intervention_status(row),
                    "hard_trim_calls": (row.get("context_intervention") or {}).get("hard_trim_calls")})
    table = ""
    for name, item in payload["summary"].items():
        table += "<tr>" + "".join("<td>" + escape(str(v)) + "</td>" for v in (
            name, f"{item['tasks']} / {item['planned_tasks']}", item["verified_pass"], item["verified_fail"], item["unverified"],
            str(item["unsupported_cells"]) + " / fallback tasks: " + str(item["fallback_tasks"]), item["total_tokens"] if item["usage_complete"] else "unknown (known: " + str(item["known_tokens"]) + ")",
            item["cached_input_tokens_known"] if item["cache_usage_complete"] else "partial / unknown",
            item["mean_duration_ms"], f'{item["intervention_observed_tasks"]} / {item["intervention_none_tasks"]} / {item["intervention_unknown_tasks"]}', json.dumps(item["paired_vs_current"]))) + "</tr>"
    cells = "".join("<details><summary>" + escape(f'{c["policy"]["name"]} / repeat {c["repeat"]} / {c["status"]}') +
                    "</summary><pre>" + escape(json.dumps(c, ensure_ascii=False, indent=2, default=str)) + "</pre></details>" for c in payload["cells"])
    document = """<!doctype html><html lang="zh"><meta charset="utf-8"><title>Context policy experiment</title>
<style>body{font:15px system-ui;max-width:1400px;margin:48px auto;padding:0 24px;color:#14213d;background:#f7f9fc}h1{font-size:32px}table{border-collapse:collapse;width:100%;background:white}td,th{padding:12px;border-bottom:1px solid #dce3ed;text-align:left}th{background:#e8eef8}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#eef2f7;padding:20px}details{margin:16px 0}p{max-width:950px;line-height:1.7}</style>
<h1>Context policy experiment</h1>"""
    document += "<p>" + escape(payload["interpretation"]) + "</p>"
    document += "<p>Run: " + escape(payload["id"]) + " · Status: " + escape(payload["status"]) + "</p>"
    document += "<table><thead><tr>" + "".join("<th>" + x + "</th>" for x in ("Policy", "Eligible / Planned", "Pass", "Fail", "Unknown", "Unsupported", "Total tokens", "Cached input", "Mean ms", "Intervention yes / no / unknown", "Paired vs current")) + "</tr></thead><tbody>" + table + "</tbody></table>" + cells + "</html>"
    (directory / "report.html").write_text(document)


def _apply_harness_ledger(cell: dict, result: dict, expected_ids: list[str]) -> list[str]:
    rows = result.get("rows")
    if not isinstance(rows, list) or any(not isinstance(row, dict) or row.get("id") not in expected_ids for row in rows):
        raise ValueError("Harness returned an invalid task ledger")
    observed_ids = [row["id"] for row in rows]
    if len(set(observed_ids)) != len(observed_ids):
        raise ValueError("Harness returned duplicate task rows")
    cell["rows"] = deepcopy(rows)
    for key in ("runner", "manifest", "manifest_after", "eval_id"):
        if key in result:
            cell[key] = deepcopy(result[key])
    return [identifier for identifier in expected_ids if identifier not in observed_ids]


def _latest_harness_ledger(harness) -> dict | None:
    result = getattr(harness, "latest_result", None)
    if isinstance(result, dict):
        return result
    path = getattr(harness, "ledger_path", None)
    if path is not None:
        try:
            result = json.loads(Path(path).read_text(encoding="utf-8"))
            return result if isinstance(result, dict) else None
        except (OSError, ValueError, TypeError):
            pass
    return None


def _fill_missing_cell_rows(cell: dict, reason: str, *, error_type: str = "") -> None:
    recorded = {row["id"] for row in cell.get("rows", [])}
    cell.setdefault("rows", []).extend(
        _unknown_row(identifier, reason, error_type=error_type)
        for identifier in cell["expected_task_ids"] if identifier not in recorded)


def run_matrix(*, policies=("current", "masking", "adaptive", "native"), runner_mode="scripted",
               benchmark_path=DEFAULT_BENCHMARK_PATH, eval_root=".evals/context",
               task_id=None, repeat=1, seed=17, max_reasoning_steps=30,
               limits=None, harness_factory=CodingBenchmarkHarness) -> dict:
    if type(repeat) is not int or not 1 <= repeat <= 100:
        raise ValueError("repeat must be between 1 and 100")
    if runner_mode not in {"scripted", "real"}:
        raise ValueError("runner must be scripted or real")
    if not isinstance(policies, (list, tuple)):
        raise ValueError("policies must be an array of names or policy objects")
    if type(max_reasoning_steps) is not int or max_reasoning_steps < 1:
        raise ValueError("max_reasoning_steps must be a positive integer")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    parsed = [asdict(context_policy(SimpleNamespace(metadata={"context_policy": p}))) for p in policies]
    if not parsed or len({p["name"] for p in parsed}) != len(parsed):
        raise ValueError("Select at least one policy; names must be unique within a matrix")
    defaults = {"max_model_attempts": 40, "max_tool_attempts": 80, "max_known_tokens": 200000, "max_seconds": 300}
    if limits is not None and (not isinstance(limits, dict) or set(limits) - set(defaults)
                               or any(type(v) is not int or v < 1 for v in limits.values())):
        raise ValueError("Execution budget limits must be positive integers with known keys")
    limits = {**defaults, **(limits or {})}
    tasks = load_benchmark(benchmark_path)
    if task_id is not None:
        tasks = [task for task in tasks if task.id == task_id]
        if not tasks:
            raise ValueError(f"Unknown benchmark task id: {task_id}")
    expected_ids = [task.id for task in tasks]
    identifier = "context-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:6]
    directory = Path(eval_root) / identifier
    directory.mkdir(parents=True, exist_ok=False)
    config = {"policies": parsed, "runner_mode": runner_mode, "repeat": repeat, "seed": seed,
              "task_id": task_id, "selected_ids": expected_ids, "max_reasoning_steps": max_reasoning_steps, "execution_limits": limits}
    payload = {"schema_version": 1, "id": identifier, "directory": str(directory.resolve()), "status": "running",
               "configuration": config, "manifest": evaluation_manifest(dataset_path=Path(benchmark_path), configuration=config),
               "cells": [], "interpretation": (
                   "Scripted runs verify plumbing and deterministic behavior only; they cannot establish model quality or API cost. "
                   "Real runs use the same fixtures, tools, configured model route and per-task budgets, with shuffled policy order. Actual model choices are recorded separately; cross-model pairs are excluded. "
                   "Unknown verification and unsupported native capability remain explicit. Known token totals include root-scoped model calls; "
                   "missing usage is unknown, not free. Planned completion rates retain every requested task; an entirely unsupported policy has no measured pass rate. Cost totals include fallback paths. "
                   "Model identities describe completed traced calls, not untraced helper calls. Cached-token observations do not imply a dollar estimate. "
                   "Policy selection alone is not an intervention: submitted masks, dropped groups, snapshot use, or committed native compaction must be observed. Missing request audit is unknown. "
                   "Pairs require equal available code/fixture/dataset fingerprints before and after each cell, plus equal execution configuration except policy. This is an explicit subset, not a complete environment lock. "
                   "Unintervened tasks cannot establish compression gains; emergency trimming is a separate confound. Planner token estimates are not billed savings. "
                   "Paired counts are descriptive; small fixtures cannot prove a best policy or long-horizon improvement.")}
    rng = random.Random(seed)
    for number in range(repeat):
        order = list(parsed)
        rng.shuffle(order)
        payload["cells"].extend({"policy": policy, "repeat": number + 1, "runner_mode": runner_mode,
                                 "status": "pending", "rows": [], "expected_task_ids": list(expected_ids)}
                                for policy in order)
    try:
        _write_report(directory, payload)
        for cell in payload["cells"]:
            policy = cell["policy"]
            cell["status"] = "running"
            _write_report(directory, payload)
            if runner_mode == "scripted" and policy["name"] == "native":
                cell.update(status="unsupported", reason="ScriptedProvider has no native Responses capability",
                            rows=[_unknown_row(identifier, "native_capability_unsupported", unsupported=True) for identifier in expected_ids])
            else:
                harness = None
                def checkpoint_progress(event):
                    if event.get("event") not in {"eval_started", "task_finished", "eval_finished"}:
                        return
                    latest = _latest_harness_ledger(harness)
                    if latest is not None:
                        _apply_harness_ledger(cell, latest, expected_ids)
                        _write_report(directory, payload)
                try:
                    harness = harness_factory(benchmark_path=benchmark_path, eval_root=directory / "cells",
                        runner_mode=runner_mode, task_id=task_id, max_reasoning_steps=max_reasoning_steps,
                        context_policy=policy, execution_budget_limits=limits, progress=checkpoint_progress)
                    result = harness.run()
                    missing = _apply_harness_ledger(cell, result, expected_ids)
                    _fill_missing_cell_rows(cell, "harness_omitted_task")
                    rows = cell["rows"]
                    unsupported = bool(rows) and all(r.get("error_type") == "NativeContextUnsupported" or
                        (r.get("policy_observation", {}).get("resolution") or {}).get("unsupported") for r in rows)
                    cell["status"] = "error" if missing else "unsupported" if unsupported else "completed"
                except BaseException as exc:
                    latest = _latest_harness_ledger(harness)
                    if latest is not None:
                        try:
                            _apply_harness_ledger(cell, latest, expected_ids)
                        except ValueError:
                            # Keep previously validated incremental rows if a
                            # later malformed receipt prevents finalization.
                            pass
                    interrupted = not isinstance(exc, Exception)
                    cell.update(status="interrupted" if interrupted else "error",
                                error_type=type(exc).__name__, reason=str(exc))
                    _fill_missing_cell_rows(cell, "evaluation_interrupted" if interrupted else "harness_error",
                                            error_type=type(exc).__name__)
                    _write_report(directory, payload)
                    if interrupted:
                        raise
            _write_report(directory, payload)
        models = {json.dumps(c.get("runner"), sort_keys=True) for c in payload["cells"] if c.get("runner")}
        payload["same_runner_configuration"] = len(models) == 1 if models else None
        actual = {(call.get("model"), call.get("profile"), call.get("provider")) for c in payload["cells"] for r in c.get("rows", []) for call in r.get("actual_model_calls", [])}
        payload["actual_models"] = [list(item) for item in sorted(actual, key=str)]
        model_rows = [r for c in payload["cells"] for r in c.get("rows", [])
                      if not ((r.get("policy_observation") or {}).get("resolution") or {}).get("unsupported")]
        payload["actual_model_observations_complete"] = bool(model_rows) and all(_observed_models(r) is not None for r in model_rows)
        payload["observed_model_scope"] = "completed traced calls only"
        payload["same_observed_model"] = len(actual) == 1 if payload["actual_model_observations_complete"] else None
        payload["root_model_identity_complete"] = bool(model_rows) and all(_model_scope_complete(r) for r in model_rows)
        payload["same_actual_model"] = payload["same_observed_model"] if payload["root_model_identity_complete"] else None
        payload["status"] = "completed_with_errors" if any(c["status"] == "error" for c in payload["cells"]) else "completed"
    except BaseException as exc:
        payload["status"] = "interrupted"
        for cell in payload["cells"]:
            if cell["status"] in {"running", "pending"}:
                active = cell["status"] == "running"
                cell["status"] = "interrupted" if active else "not_run"
                _fill_missing_cell_rows(cell, "evaluation_interrupted" if active else "not_run_after_interruption",
                                        error_type=type(exc).__name__)
        raise
    finally:
        _write_report(directory, payload)
    return payload


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runner", choices=("scripted", "real"), default="scripted")
    parser.add_argument("--policy", choices=POLICY_NAMES, action="append")
    parser.add_argument("--policy-config", type=Path, help="JSON array of policy objects; overrides --policy")
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK_PATH)
    parser.add_argument("--eval-root", type=Path, default=Path(".evals/context"))
    parser.add_argument("--task-id")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--max-model-calls", type=int, default=40)
    parser.add_argument("--max-tool-calls", type=int, default=80)
    parser.add_argument("--max-tokens", type=int, default=200000)
    parser.add_argument("--max-seconds", type=int, default=300)
    args = parser.parse_args(argv)
    policies = json.loads(args.policy_config.read_text()) if args.policy_config else args.policy or ("current", "masking", "adaptive", "native")
    result = run_matrix(policies=policies, runner_mode=args.runner, benchmark_path=args.benchmark,
        eval_root=args.eval_root, task_id=args.task_id, repeat=args.repeat, seed=args.seed,
        max_reasoning_steps=args.max_steps, limits={"max_model_attempts": args.max_model_calls,
        "max_tool_attempts": args.max_tool_calls, "max_known_tokens": args.max_tokens, "max_seconds": args.max_seconds})
    print(json.dumps({"directory": result["directory"], "summary": result["summary"]}, ensure_ascii=False))
    return int(any(c["status"] == "error" for c in result["cells"]))


if __name__ == "__main__":
    raise SystemExit(main())
