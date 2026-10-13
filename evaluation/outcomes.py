"""Evaluation evidence is independent from the agent's execution lifecycle."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any


def finish_generation(run_state: Any, reply: str) -> None:
    """Finalize generation without erasing an explicit runtime stop/failure."""
    if run_state.status in {"failed", "stopped", "cancelled"}:
        return
    task = run_state.metadata.get("coding_application") or {}
    reason = task.get("execution_status")
    if reason and reason != "completed":
        run_state.stop(str(reason), reply)
    else:
        run_state.finish_success(reply)


def patch_digest(patch: str | bytes) -> str:
    data = patch.encode("utf-8") if isinstance(patch, str) else patch
    return hashlib.sha256(data).hexdigest()


def verification_result(
    outcome: str = "not_run", *, reason: str = "not_evaluated", **evidence: Any,
) -> dict[str, Any]:
    if outcome not in {"pass", "fail", "not_run", "inconclusive"}:
        raise ValueError(f"Unknown verification outcome: {outcome}")
    return {"outcome": outcome, "reason": reason, **evidence}


def verification_outcome(row: dict[str, Any]) -> str:
    value = row.get("verification")
    if value is None:
        return "not_run"
    if not isinstance(value, dict):
        return "inconclusive"
    outcome = value.get("outcome")
    return outcome if isinstance(outcome, str) and outcome in {"pass", "fail", "not_run", "inconclusive"} else "inconclusive"


def import_official_verification(
    *, report_root: Path, instance_id: str, model_name: str, expected_patch_sha256: str,
) -> dict[str, Any]:
    """Read one official result only when its saved patch matches our submission.

    report_root must be the caller's specific official run directory. A harness
    exit code or an aggregate resolved count is never enough evidence. The
    report and patch files are local trusted grader outputs, not agent output.
    """
    evidence = {"verifier_id": "swebench.official", "patch_sha256": expected_patch_sha256}
    if expected_patch_sha256 == patch_digest(b""):
        return verification_result(reason="empty_patch", **evidence)
    if not report_root.is_dir():
        return verification_result("inconclusive", reason="official_report_missing", **evidence)
    model_dir = model_name.replace("/", "__")
    matches = [
        path for path in report_root.rglob("report.json")
        if path.parent.name == instance_id and path.parent.parent.name == model_dir
    ]
    if len(matches) != 1:
        reason = "official_report_missing" if not matches else "official_report_ambiguous"
        return verification_result("inconclusive", reason=reason, **evidence)
    report_path = matches[0]
    try:
        applied_patch = (report_path.parent / "patch.diff").read_bytes()
        if patch_digest(applied_patch) != expected_patch_sha256:
            return verification_result("inconclusive", reason="official_patch_mismatch", **evidence)
        payload = json.loads(report_path.read_text(encoding="utf-8"))
        result = payload.get(instance_id) if isinstance(payload, dict) else None
        if not isinstance(result, dict) or type(result.get("resolved")) is not bool:
            return verification_result("inconclusive", reason="official_report_invalid", **evidence)
        # An unapplied patch / missing tests is an infrastructure or grading
        # uncertainty, not an observed model test failure.
        if result.get("patch_applied") is False or result.get("test_status") == "ERROR":
            return verification_result("inconclusive", reason="official_grading_incomplete", **evidence)
        return verification_result(
            "pass" if result["resolved"] else "fail", reason="official_tests",
            report_path=str(report_path), report_sha256=patch_digest(report_path.read_bytes()),
            **evidence,
        )
    except (OSError, ValueError, TypeError) as exc:
        return verification_result(
            "inconclusive", reason="official_evidence_unreadable",
            error_type=type(exc).__name__, **evidence,
        )


def token_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Unknown and partial usage stays unknown; known usage is a lower bound."""
    known = 0
    incomplete = 0
    for row in rows:
        metrics = row.get("metrics") or {}
        total = metrics.get("total_tokens")
        complete = (
            isinstance(total, (int, float)) and not isinstance(total, bool) and math.isfinite(total) and total >= 0
            and metrics.get("usage_status") not in {"unknown", "partial"}
            and not metrics.get("unknown_usage_calls")
        )
        subtotal = total if complete else metrics.get("known_total_tokens")
        if isinstance(subtotal, (int, float)) and not isinstance(subtotal, bool) and math.isfinite(subtotal) and subtotal >= 0:
            known += int(subtotal)
        if not complete:
            incomplete += 1
    return {
        "total_tokens": known if rows and not incomplete else None,
        "known_total_tokens": known,
        "unknown_usage_rows": incomplete,
        "usage_status": "complete" if rows and not incomplete else "partial" if known else "unknown",
    }
