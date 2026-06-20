from __future__ import annotations

from plugins.base import Plugin


class EvalReportPlugin(Plugin):
    name = "eval_report"

    def after_eval(self, context) -> None:
        write_eval_report(context.eval_dir, context.payload)


def write_eval_report(eval_dir, payload: dict) -> None:
    summary = payload.get("summary", {})
    runner = payload.get("runner", {})
    benchmark = payload.get("benchmark", {})
    rows = payload.get("rows", [])
    lines = [
        "# Coding Agent Benchmark",
        "",
        "## Summary",
        "",
        f"- Eval ID: `{payload.get('eval_id', '')}`",
        f"- Runner: `{runner.get('mode', '')}` / `{runner.get('model', '')}`",
        f"- Benchmark: `{benchmark.get('source', '')}`",
        f"- Task filter: `{benchmark.get('task_id') or 'all'}`",
        f"- Workspace retained: `{payload.get('workspace_retained', False)}`",
        f"- Workspace root: `{payload.get('workspace_root', '')}`",
        "",
        f"- Tasks: {summary.get('total_tasks', 0)}",
        f"- Passed: {summary.get('passed', 0)}",
        f"- Failed: {summary.get('failed', 0)}",
        f"- Pass rate: {summary.get('pass_rate', 0):.2%}",
        f"- Verifier pass rate: {summary.get('verifier_pass_rate', 0):.2%}",
        f"- Workspace diff pass rate: {summary.get('workspace_diff_pass_rate', 0):.2%}",
        f"- Trace completeness rate: {summary.get('trace_completeness_rate', 0):.2%}",
        f"- Avg reasoning steps: {summary.get('avg_reasoning_steps', 0):.2f}",
        f"- Avg tool calls: {summary.get('avg_tool_calls', 0):.2f}",
        "",
        "## Failure Categories",
        "",
    ]
    failure_counts = summary.get("failure_category_counts") or {}
    if failure_counts:
        for name, count in sorted(failure_counts.items()):
            lines.append(f"- `{name}`: {count}")
    else:
        lines.append("- none")

    lines.extend([
        "",
        "## Rows",
        "",
        "| id | category | status | failure | reason | steps | tools |",
        "| --- | --- | --- | --- | --- | ---: | ---: |",
    ])
    for row in rows:
        lines.append(
            "| {id} | {category} | {status} | {failure} | {reason} | {steps} | {tools} |".format(
                id=row.get("id", ""),
                category=row.get("category", ""),
                status=row.get("status", ""),
                failure=row.get("failure_category", ""),
                reason=row.get("failure_reason", ""),
                steps=row.get("reasoning_steps", 0),
                tools=row.get("tool_calls", 0),
            )
        )

    failed = [row for row in rows if not row.get("passed")]
    if failed:
        lines.extend(["", "## Failed Tasks", ""])
        for row in failed:
            lines.extend([
                f"### {row.get('id', '')}",
                "",
                f"- Failure: `{row.get('failure_category', '')}`",
                f"- Reason: `{row.get('failure_reason', '')}`",
                f"- Run dir: `{row.get('run_dir', '')}`",
                f"- Workspace: `{row.get('workspace_path', '')}`",
                "",
            ])
            for check in row.get("verifier", {}).get("checks", []):
                if not check.get("passed"):
                    label = check.get("name", "check")
                    detail = check.get("path") or check.get("tool") or check.get("event") or check.get("command") or ""
                    lines.append(f"- failed `{label}` {detail}")
            lines.append("")

    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
