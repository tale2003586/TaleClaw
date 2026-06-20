from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from runtime.trace.failure import FailureClassification, classify_failure
from runtime.failure_reasons import SUBAGENT_TERMINAL_REASONS


EVIDENCE_GATHERING_TOOLS = {
    "bash",
    "rg",
    "grep",
    "nl",
    "read_file",
    "list_files",
    "repo_map",
    "code_outline",
    "git_status",
    "git_diff",
    "git_log",
}

COMPLETION_DECLARATION_PATTERNS = (
    r"已(?:经)?(?:补齐|完成|覆盖).*(?:主链路|核心|线索|任务|reasoning|工具执行)",
    r"(?:主链路|核心.*链路|required clues).*(?:已|已经).*(?:补齐|完成|覆盖)",
    r"现在(?:给出|直接)?(?:最终)?总结",
    r"直接(?:总结|给出结论|收尾)",
    r"可以(?:总结|收尾|给出最终答案)",
    r"\bready to (?:answer|summari[sz]e|finalize)\b",
    r"\b(?:now|next).*(?:final answer|summary)\b",
    r"\b(?:core|main).*(?:complete|covered)\b",
)

COMPLETION_NEGATION_PATTERNS = (
    r"未完成",
    r"尚未",
    r"还没有",
    r"不能.*(?:总结|收尾|完成)",
    r"\bnot (?:complete|ready|covered)\b",
    r"\binsufficient\b",
)


def write_trace_summary(run_dir: Path, *, external_logs: list[str] | None = None) -> tuple[Path, Path]:
    run_state = _read_json(run_dir / "run_state.json")
    metrics = _read_json(run_dir / "metrics.json")
    report_payload = _read_json(run_dir / "report.json")
    report = report_payload.get("report", {}) if isinstance(report_payload.get("report"), dict) else {}
    events = _read_events(run_dir / "trace.jsonl")
    classification = classify_failure(
        run_state=run_state,
        events=events,
        report=report,
        external_logs=external_logs,
    )
    payload = build_trace_summary_payload(
        run_state=run_state,
        metrics=metrics,
        report=report,
        events=events,
        classification=classification,
    )
    json_path = run_dir / "trace_summary.json"
    md_path = run_dir / "trace_summary.md"
    json_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_trace_summary_markdown(payload), encoding="utf-8")
    return md_path, json_path


def build_trace_summary_payload(
    *,
    run_state: dict[str, Any],
    metrics: dict[str, Any],
    report: dict[str, Any],
    events: list[dict[str, Any]],
    classification: FailureClassification | None = None,
) -> dict[str, Any]:
    classification = classification or classify_failure(
        run_state=run_state,
        events=events,
        report=report,
    )
    workspace = _workspace_summary(events, report)
    tool_summary = _tool_summary(events)
    file_summary = _file_summary(events)
    verification = _verification_summary(report)
    multi_agent = _multi_agent_summary(events)
    subagents = _subagent_summary(events)
    memory = _memory_summary(events)
    perfectionism = _perfectionism_summary(events)
    timeline = _tool_timeline(events)
    execution_path = _execution_path(events, run_state)
    return {
        "schema_version": 1,
        "run_id": run_state.get("run_id", ""),
        "session_id": run_state.get("session_id", ""),
        "task_id": _task_id(run_state, report),
        "status": run_state.get("status", ""),
        "stop_reason": run_state.get("stop_reason", ""),
        "failure": classification.to_dict(),
        "metrics": {
            "reasoning_steps": run_state.get("reasoning_steps", 0),
            "tool_calls": run_state.get("tool_calls", 0),
            "model_calls": metrics.get("model_calls", 0),
            "model_failures": metrics.get("model_failures", 0),
            "tool_failures": metrics.get("tool_failures", 0),
            "tool_denials": metrics.get("tool_denials", 0),
            "duplicate_tool_call_ratio": metrics.get("duplicate_tool_call_ratio", 0),
            "truncated_tool_output_count": metrics.get("truncated_tool_output_count", 0),
            "post_completion_tool_calls": perfectionism["post_completion_tool_calls"],
            "evidence_gathering_steps": perfectionism["evidence_gathering_steps"],
            "subagent_incomplete_count": metrics.get("subagent_incomplete_count", 0),
            "subagent_fanout_count": metrics.get("subagent_fanout_count", 0),
            "subagent_retry_count": subagents["retry_count"],
            "subagent_degrade_count": subagents["degrade_count"],
            "fanout_rejected_count": subagents["fanout_rejected_count"],
            "dispatch_rejected_count": subagents["dispatch_rejected_count"],
            "subagent_missing_file_count": subagents["missing_file_count"],
            "subagent_infeasible_count": subagents["infeasible_count"],
            "subagent_recovered_count": subagents["recovered_count"],
            "total_tokens": metrics.get("total_tokens", 0),
            "duration_ms": metrics.get("run_duration_ms", 0),
        },
        "workspace": workspace,
        "tools": tool_summary,
        "files": file_summary,
        "verification": verification,
        "multi_agent": multi_agent,
        "memory": memory,
        "execution_path": execution_path,
        "timeline": timeline,
    }


def render_trace_summary_markdown(summary: dict[str, Any]) -> str:
    failure = summary.get("failure") or {}
    metrics = summary.get("metrics") or {}
    workspace = summary.get("workspace") or {}
    tools = summary.get("tools") or {}
    files = summary.get("files") or {}
    verification = summary.get("verification") or {}
    multi_agent = summary.get("multi_agent") or {}
    memory = summary.get("memory") or {}
    lines = [
        "# Trace Summary",
        "",
        "## 结果",
        "",
        f"- Run ID: `{_value(summary.get('run_id'))}`",
        f"- Task ID: `{_value(summary.get('task_id'))}`",
        f"- Status: `{_value(summary.get('status'))}`",
        f"- Stop Reason: `{_value(summary.get('stop_reason'))}`",
        f"- Failure Category: `{_value(failure.get('category'))}`",
        f"- Failure Reason: {_value(failure.get('reason'))}",
        "",
        "## 执行路径",
        "",
        _execution_path_markdown(summary.get("execution_path") or []),
        "",
        "## 指标",
        "",
        f"- Reasoning Steps: {_value(metrics.get('reasoning_steps'))}",
        f"- Model Calls: {_value(metrics.get('model_calls'))}",
        f"- Tool Calls: {_value(metrics.get('tool_calls'))}",
        f"- Tool Denials: {_value(metrics.get('tool_denials'))}",
        f"- Duplicate Tool Call Ratio: {_value(metrics.get('duplicate_tool_call_ratio'))}",
        f"- Truncated Tool Outputs: {_value(metrics.get('truncated_tool_output_count'))}",
        f"- Post-completion Tool Calls: {_value(metrics.get('post_completion_tool_calls'))}",
        f"- Evidence Gathering Steps: {_value(metrics.get('evidence_gathering_steps'))}",
        f"- Subagent Incomplete: {_value(metrics.get('subagent_incomplete_count'))}",
        f"- Subagent Fan-out: {_value(metrics.get('subagent_fanout_count'))}",
        f"- Subagent Retries: {_value(metrics.get('subagent_retry_count'))}",
        f"- Subagent Degrades: {_value(metrics.get('subagent_degrade_count'))}",
        f"- Fan-out Rejected: {_value(metrics.get('fanout_rejected_count'))}",
        f"- Dispatch Rejected: {_value(metrics.get('dispatch_rejected_count'))}",
        f"- Subagent Missing Files: {_value(metrics.get('subagent_missing_file_count'))}",
        f"- Subagent Infeasible: {_value(metrics.get('subagent_infeasible_count'))}",
        f"- Subagent Recovered: {_value(metrics.get('subagent_recovered_count'))}",
        f"- Tokens: {_value(metrics.get('total_tokens'))}",
        f"- Duration: {_value(metrics.get('duration_ms'))} ms",
        "",
        "## Workspace",
        "",
        f"- Requested: `{_value(workspace.get('requested'))}`",
        f"- Resolved: `{_value(workspace.get('resolved'))}`",
        f"- Allowed Root: `{_value(workspace.get('allowed_root'))}`",
        f"- Escape Attempts: {_value(workspace.get('escape_attempts'))}",
        "",
        "## 工具",
        "",
        f"- Total: {_value(tools.get('total'))}",
        f"- Denied: {_value(tools.get('denied'))}",
        f"- Failed: {_value(tools.get('failed'))}",
        f"- Loop Guard Denials: {_value(tools.get('loop_guard_denials'))}",
        f"- Workspace Denials: {_value(tools.get('workspace_denials'))}",
        f"- Names: `{', '.join(tools.get('names') or [])}`",
        "",
        "## 文件",
        "",
        f"- Read: `{', '.join(files.get('read') or [])}`",
        f"- Modified: `{', '.join(files.get('modified') or [])}`",
        f"- Created: `{', '.join(files.get('created') or [])}`",
        f"- Deleted: `{', '.join(files.get('deleted') or [])}`",
        f"- Unexpected Modified: `{', '.join(files.get('unexpected_modified') or [])}`",
        "",
        "## 验证",
        "",
        f"- Passed: `{_value(verification.get('passed'))}`",
        f"- Commands: `{', '.join(verification.get('commands') or [])}`",
        f"- Failed Checks: `{', '.join(verification.get('failed_checks') or [])}`",
        "",
        "## 多 Agent",
        "",
        f"- Assignments Created: {_value(multi_agent.get('assignments_created'))}",
        f"- Assignments Completed: {_value(multi_agent.get('assignments_completed'))}",
        f"- Findings Used: {_value(multi_agent.get('findings_used'))}",
        f"- Conflicts Resolved: {_value(multi_agent.get('conflicts_resolved'))}",
        "",
        "## 记忆处理",
        "",
        f"- Candidate Evaluations: {_value(memory.get('candidate_evaluations'))}",
        f"- Candidate Upserts: {_value(memory.get('candidate_upserts'))}",
        f"- Candidate Promotions: {_value(memory.get('candidate_promotions'))}",
        f"- History Vector Upserts: {_value(memory.get('history_vector_upserts'))}",
        f"- History Vector Failures: {_value(memory.get('history_vector_failures'))}",
        f"- Last Similar Hit Count: {_value(memory.get('last_similar_hit_count'))}",
        f"- Last Candidate Selected: `{_value(memory.get('last_candidate_selected'))}`",
        "",
        "## 关键时间线",
        "",
        _timeline_table(summary.get("timeline") or []),
        "",
    ]
    evidence = failure.get("evidence") or []
    if evidence:
        lines.extend(["## 失败证据", ""])
        lines.extend(f"- {item}" for item in evidence)
        lines.append("")
    return "\n".join(lines)


def _workspace_summary(events: list[dict[str, Any]], report: dict[str, Any]) -> dict[str, Any]:
    resolved = _first_payload(events, "workspace.resolved")
    escape_attempts = 0
    commands_with_external_cd = []
    for event in events:
        payload = event.get("payload") or {}
        for hook in (payload.get("pre_hook_trace") or []):
            if hook.get("hook_name") in {"shell_workspace_scope", "file_write_scope"} and hook.get("decision") == "deny":
                escape_attempts += 1
                commands_with_external_cd.append(str(payload.get("final_arguments_preview") or ""))
    return {
        "requested": resolved.get("workspace_requested") or report.get("workspace_root") or "",
        "resolved": resolved.get("workspace_root") or report.get("workspace_root") or "",
        "display_name": resolved.get("workspace_display_name", ""),
        "allowed_root": resolved.get("workspace_allowed_root", ""),
        "source": resolved.get("workspace_source", ""),
        "escape_attempts": escape_attempts,
        "commands_with_external_cd": commands_with_external_cd,
    }


def _tool_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    names = set()
    denied = failed = loop_denials = workspace_denials = 0
    total = 0
    for event in events:
        if event.get("event") not in {"tool.call.completed", "tool.call.failed"}:
            continue
        total += 1
        payload = event.get("payload") or {}
        tool_name = str(payload.get("tool_name") or "")
        if tool_name:
            names.add(tool_name)
        status = str(payload.get("status") or "")
        if status == "denied":
            denied += 1
        if status == "error" or event.get("event") == "tool.call.failed":
            failed += 1
        for hook in (payload.get("pre_hook_trace") or []):
            if hook.get("decision") != "deny":
                continue
            if hook.get("hook_name") == "tool_loop_guard":
                loop_denials += 1
            if hook.get("hook_name") in {"shell_workspace_scope", "file_write_scope"}:
                workspace_denials += 1
    return {
        "total": total,
        "denied": denied,
        "failed": failed,
        "loop_guard_denials": loop_denials,
        "workspace_denials": workspace_denials,
        "names": sorted(names),
    }


def _perfectionism_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    completion_declared = False
    post_completion_tool_calls = 0
    evidence_steps = set()
    for event in events:
        name = str(event.get("event") or "")
        payload = event.get("payload") or {}
        if name == "model.call.completed":
            content = str(payload.get("content_preview") or "")
            if _declares_completion(content):
                completion_declared = True
        elif name in {"tool.call.completed", "tool.call.failed"}:
            tool_name = str(payload.get("tool_name") or "")
            if tool_name in EVIDENCE_GATHERING_TOOLS:
                step = event.get("step")
                if step is not None:
                    evidence_steps.add(step)
            if completion_declared:
                post_completion_tool_calls += 1
    return {
        "post_completion_tool_calls": post_completion_tool_calls,
        "evidence_gathering_steps": len(evidence_steps),
    }


def _declares_completion(content: str) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    lowered = text.lower()
    if any(re.search(pattern, lowered, re.IGNORECASE) for pattern in COMPLETION_NEGATION_PATTERNS):
        return False
    return any(
        re.search(pattern, text, re.IGNORECASE)
        for pattern in COMPLETION_DECLARATION_PATTERNS
    )


def _subagent_summary(events: list[dict[str, Any]]) -> dict[str, int]:
    retry_count = 0
    recovered_count = 0
    degrade_count = 0
    infeasible_count = 0
    fanout_rejected_count = 0
    dispatch_rejected_count = 0
    missing_file_count = 0
    for event in events:
        name = str(event.get("event") or "")
        payload = event.get("payload") or {}
        if name == "subagent.retry.completed":
            retry_count += _int(payload.get("retry_count"), default=1)
            if payload.get("recovered"):
                recovered_count += 1
        elif name in {"subagent.degrade", "subagent.degraded", "subagent.degrade.completed"}:
            degrade_count += 1
        elif name == "subagent.fanout.rejected":
            fanout_rejected_count += 1
            if payload.get("dispatch_rejected"):
                dispatch_rejected_count += 1
            missing_file_count += len(payload.get("missing_paths") or [])
        elif name == "subagent.completed":
            reason = str(payload.get("failure_reason") or "")
            if reason in SUBAGENT_TERMINAL_REASONS:
                infeasible_count += 1
            if reason == "subagent_missing_required_files":
                missing_file_count += 1
            retry_count += _int(payload.get("retry_count"), default=0)
            if payload.get("recovered"):
                recovered_count += 1
    return {
        "retry_count": retry_count,
        "degrade_count": degrade_count,
        "fanout_rejected_count": fanout_rejected_count,
        "dispatch_rejected_count": dispatch_rejected_count,
        "missing_file_count": missing_file_count,
        "infeasible_count": infeasible_count,
        "recovered_count": recovered_count,
    }


def _file_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    read = set()
    modified = []
    created = []
    deleted = []
    unexpected_modified = []
    for event in events:
        if event.get("event") == "tool.call.completed":
            payload = event.get("payload") or {}
            tool_name = payload.get("tool_name")
            args = _decode_preview(payload.get("final_arguments_preview"))
            path = args.get("path") if isinstance(args, dict) else None
            if isinstance(path, str):
                if tool_name == "read_file":
                    read.add(path)
                elif tool_name in {"edit_file", "write_file"}:
                    modified.append(path)
        if event.get("event") == "workspace.diff.written":
            payload = event.get("payload") or {}
            created = list(payload.get("created_preview") or [])
            modified = list(dict.fromkeys(modified + list(payload.get("modified_preview") or [])))
            deleted = list(payload.get("deleted_preview") or [])
    return {
        "read": sorted(read),
        "modified": modified,
        "created": created,
        "deleted": deleted,
        "unexpected_modified": unexpected_modified,
    }


def _verification_summary(report: dict[str, Any]) -> dict[str, Any]:
    verifier = report.get("verifier") if isinstance(report.get("verifier"), dict) else {}
    checks = verifier.get("checks") or []
    commands = []
    failed = []
    for check in checks:
        name = str(check.get("name") or "")
        if name in {"must_pass_command", "must_fail_command"}:
            command = str((check.get("details") or {}).get("command") or "")
            if command:
                commands.append(command)
        if not check.get("passed"):
            failed.append(name)
    return {
        "passed": verifier.get("passed", report.get("verifier_passed", "")),
        "commands": commands,
        "failed_checks": failed,
    }


def _multi_agent_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    assignments_created = sum(1 for event in events if event.get("event") == "agent.assignment.created")
    assignments_completed = sum(1 for event in events if event.get("event") == "agent.assignment.completed")
    synthesis = [_payload for _payload in (_event.get("payload") or {} for _event in events) if isinstance(_payload, dict) and _payload.get("used_assignments")]
    findings_used = sum(len(item.get("used_assignments") or []) for item in synthesis)
    conflicts_resolved = sum(len(item.get("resolved_conflicts") or []) for item in synthesis)
    return {
        "assignments_created": assignments_created,
        "assignments_completed": assignments_completed,
        "findings_used": findings_used,
        "conflicts_resolved": conflicts_resolved,
    }


def _memory_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    candidate_evaluations = 0
    candidate_upserts = 0
    candidate_promotions = 0
    history_vector_upserts = 0
    history_vector_failures = 0
    lifecycle_completed = 0
    last_similar_hit_count = None
    last_candidate_selected = None
    promoted_previews = []
    for event in events:
        name = event.get("event")
        payload = event.get("payload") or {}
        if name == "memory.candidate.evaluated":
            candidate_evaluations += 1
            last_similar_hit_count = payload.get("similar_hit_count")
            last_candidate_selected = payload.get("candidate_selected")
        elif name == "memory.candidate.upserted":
            candidate_upserts += 1
        elif name == "memory.candidate.promoted":
            candidate_promotions += 1
            preview = payload.get("memory_text_preview")
            if preview:
                promoted_previews.append(str(preview))
        elif name == "memory.history_vector.upserted":
            history_vector_upserts += 1
        elif name == "memory.history_vector.failed":
            history_vector_failures += 1
        elif name == "memory.lifecycle.completed":
            lifecycle_completed += 1
    return {
        "candidate_evaluations": candidate_evaluations,
        "candidate_upserts": candidate_upserts,
        "candidate_promotions": candidate_promotions,
        "history_vector_upserts": history_vector_upserts,
        "history_vector_failures": history_vector_failures,
        "lifecycle_completed": lifecycle_completed,
        "last_similar_hit_count": last_similar_hit_count,
        "last_candidate_selected": last_candidate_selected,
        "promoted_previews": promoted_previews[-5:],
    }


def _tool_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline = []
    for event in events:
        if event.get("event") not in {"model.call.completed", "model.call.failed", "tool.call.completed", "tool.call.failed"}:
            continue
        payload = event.get("payload") or {}
        timeline.append({
            "step": event.get("step"),
            "event": event.get("event"),
            "name": payload.get("tool_name") or payload.get("model") or "",
            "status": payload.get("status") or ("failed" if str(event.get("event")).endswith(".failed") else "completed"),
            "duration_ms": payload.get("duration_ms"),
            "preview": payload.get("output_preview") or payload.get("content_preview") or payload.get("error_message") or "",
        })
    return timeline[:40]


def _execution_path(events: list[dict[str, Any]], run_state: dict[str, Any]) -> list[dict[str, Any]]:
    path = []
    last_step = None
    for event in events:
        name = str(event.get("event") or "")
        payload = event.get("payload") or {}
        step = event.get("step")
        if name == "reasoning.step.started":
            last_step = step
            path.append({
                "kind": "plan",
                "step": step,
                "label": f"第 {step} 轮：构建上下文并请求模型",
                "status": "started",
                "detail": f"message_count={payload.get('message_count', '-')}",
            })
        elif name == "model.call.completed":
            tool_calls = payload.get("tool_calls") or []
            content = str(payload.get("content_preview") or "").strip()
            if tool_calls:
                call_names = [str(call.get("name") or "") for call in tool_calls if call.get("name")]
                label = f"模型决定调用工具：{', '.join(call_names)}"
                detail = f"tool_call_count={len(tool_calls)}"
            elif content:
                label = "模型给出最终回复"
                detail = _preview(content, 160)
            else:
                label = "模型返回空文本"
                detail = "no content preview"
            path.append({
                "kind": "model",
                "step": step,
                "label": label,
                "status": "completed",
                "detail": detail,
            })
        elif name in {"tool.call.completed", "tool.call.failed"}:
            tool_name = str(payload.get("tool_name") or "")
            status = str(payload.get("status") or ("failed" if name.endswith(".failed") else "completed"))
            output = str(payload.get("output_preview") or payload.get("error_message") or "")
            path.append({
                "kind": "tool",
                "step": step,
                "label": f"调用工具：{tool_name}",
                "status": status,
                "detail": _preview(output, 160),
            })
        elif name == "reasoning.step.completed":
            reason = str(payload.get("reason") or "")
            if reason == "assistant_final_message":
                path.append({
                    "kind": "complete",
                    "step": step,
                    "label": "本轮对话完成",
                    "status": "completed",
                    "detail": reason,
                })
            elif payload.get("loop_guard_denied"):
                path.append({
                    "kind": "stop",
                    "step": step,
                    "label": "循环保护触发",
                    "status": "stopped",
                    "detail": "loop_guard_denied=true",
                })
        elif name == "run_stopped":
            path.append({
                "kind": "stop",
                "step": last_step,
                "label": "运行停止",
                "status": "stopped",
                "detail": str(payload.get("reason") or payload.get("message_preview") or ""),
            })
    if not any(item.get("kind") in {"complete", "stop"} for item in path):
        status = str(run_state.get("status") or "")
        if status:
            path.append({
                "kind": "complete" if status == "completed" else "stop",
                "step": run_state.get("reasoning_steps"),
                "label": "运行结束",
                "status": status,
                "detail": str(run_state.get("stop_reason") or run_state.get("error") or ""),
            })
    return path[:80]


def _execution_path_markdown(path: list[dict[str, Any]]) -> str:
    if not path:
        return "_No execution path recorded._"
    compact = []
    for item in path:
        kind = item.get("kind")
        label = str(item.get("label") or "")
        status = str(item.get("status") or "")
        if kind == "plan":
            compact.append("计划")
        elif kind == "model":
            compact.append("模型")
        elif kind == "tool":
            tool_label = label.replace("调用工具：", "")
            compact.append(f"工具:{tool_label}")
        elif kind == "complete":
            compact.append("完成")
        elif kind == "stop":
            compact.append("停止")
        else:
            compact.append(label or kind or "事件")
        if status in {"denied", "error", "failed", "stopped"} and compact:
            compact[-1] = f"{compact[-1]}({status})"
    lines = [
        "```text",
        " -> ".join(compact),
        "```",
        "",
        "| Step | Type | Status | Detail |",
        "| --- | --- | --- | --- |",
    ]
    for item in path:
        lines.append(
            "| "
            f"{_value(item.get('step'))} | "
            f"{_escape(item.get('label'))} | "
            f"{_escape(item.get('status'))} | "
            f"{_escape(item.get('detail'))} |"
        )
    return "\n".join(lines)


def _timeline_table(timeline: list[dict[str, Any]]) -> str:
    if not timeline:
        return "_No model or tool events recorded._"
    rows = [
        "| Step | Event | Name | Status | Preview |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in timeline:
        rows.append(
            "| "
            f"{_value(item.get('step'))} | "
            f"{_escape(item.get('event'))} | "
            f"{_escape(item.get('name'))} | "
            f"{_escape(item.get('status'))} | "
            f"{_escape(_preview(item.get('preview'), 180))} |"
        )
    return "\n".join(rows)


def _task_id(run_state: dict[str, Any], report: dict[str, Any]) -> str:
    metadata = run_state.get("metadata") if isinstance(run_state.get("metadata"), dict) else {}
    return str(metadata.get("benchmark_task_id") or report.get("benchmark_task_id") or run_state.get("chat_id") or "")


def _first_payload(events: list[dict[str, Any]], event_name: str) -> dict[str, Any]:
    for event in events:
        if event.get("event") == event_name:
            payload = event.get("payload")
            return payload if isinstance(payload, dict) else {}
    return {}


def _decode_preview(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _read_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _preview(value: Any, limit: int = 500) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


def _value(value: Any) -> str:
    if value is None or value == "":
        return "-"
    return str(value)


def _int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _escape(value: Any) -> str:
    return _value(value).replace("|", "\\|").replace("\n", " ")
