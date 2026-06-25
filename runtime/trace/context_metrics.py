from __future__ import annotations

from typing import Any


def context_build_metrics_from_report(
    report: Any,
    *,
    duration_ms: Any = None,
) -> dict[str, Any]:
    payload = _as_dict(report)
    sections = _section_items(payload.get("sections"))
    reductions = _list_of_dicts(payload.get("reductions"))

    reduced_sections = []
    before_chars = 0
    after_chars = 0
    saved_chars = 0
    for reduction in reductions:
        before = _int(reduction.get("before_chars"))
        after = _int(reduction.get("after_chars"))
        if before <= 0 and after <= 0:
            continue
        saved = max(0, before - after)
        before_chars += before
        after_chars += after
        saved_chars += saved
        reduced_sections.append({
            "section": str(reduction.get("section") or ""),
            "before_chars": before,
            "after_chars": after,
            "saved_chars": saved,
            "compression_ratio": _ratio(after, before, default=1.0),
            "savings_ratio": _ratio(saved, before, default=0.0),
            "strategy": str(reduction.get("strategy") or ""),
        })

    truncated_sections = []
    section_raw_chars = 0
    section_rendered_chars = 0
    for name, section in sections:
        raw = _int(section.get("raw_chars"))
        rendered = _int(section.get("rendered_chars"))
        section_raw_chars += raw
        section_rendered_chars += rendered
        truncated = bool(section.get("truncated")) or (raw > 0 and rendered < raw)
        if not truncated:
            continue
        saved = max(0, raw - rendered)
        truncated_sections.append({
            "section": name,
            "raw_chars": raw,
            "rendered_chars": rendered,
            "saved_chars": saved,
            "compression_ratio": _ratio(rendered, raw, default=1.0),
            "savings_ratio": _ratio(saved, raw, default=0.0),
            "budget_chars": section.get("budget_chars"),
            "strategy": str((section.get("metadata") or {}).get("strategy") or ""),
        })

    if before_chars <= 0 and truncated_sections:
        before_chars = sum(item["raw_chars"] for item in truncated_sections)
        after_chars = sum(item["rendered_chars"] for item in truncated_sections)
        saved_chars = sum(item["saved_chars"] for item in truncated_sections)

    compressed = saved_chars > 0
    return {
        "duration_ms": _round(duration_ms),
        "compressed": compressed,
        "compression_before_chars": before_chars,
        "compression_after_chars": after_chars,
        "compression_saved_chars": saved_chars,
        "compression_ratio": _ratio(after_chars, before_chars, default=1.0),
        "compression_savings_ratio": _ratio(saved_chars, before_chars, default=0.0),
        "reduction_count": len(reductions),
        "reduced_sections": reduced_sections,
        "truncated_sections": truncated_sections,
        "section_raw_chars": section_raw_chars,
        "section_rendered_chars": section_rendered_chars,
        "section_render_ratio": _ratio(section_rendered_chars, section_raw_chars, default=1.0),
        "final_total_chars": _int(payload.get("total_chars")),
        "budget_chars": payload.get("budget_chars"),
        "over_budget": bool(payload.get("over_budget")),
    }


def context_metric_record_from_event(event: dict[str, Any]) -> dict[str, Any]:
    payload = event.get("payload") or {}
    metrics = payload.get("context_metrics")
    if isinstance(metrics, dict):
        record = dict(metrics)
        if "duration_ms" not in record:
            record["duration_ms"] = _round(payload.get("duration_ms"))
    else:
        record = context_build_metrics_from_report(
            payload.get("context_report"),
            duration_ms=payload.get("duration_ms"),
        )
    record["step"] = event.get("step")
    record["timestamp"] = event.get("timestamp", "")
    return record


def aggregate_context_metrics(records: list[dict[str, Any]]) -> dict[str, Any]:
    build_count = len(records)
    compressed = [item for item in records if item.get("compressed")]
    durations = [_float(item.get("duration_ms")) for item in records]
    before_chars = sum(_int(item.get("compression_before_chars")) for item in compressed)
    after_chars = sum(_int(item.get("compression_after_chars")) for item in compressed)
    saved_chars = sum(_int(item.get("compression_saved_chars")) for item in compressed)
    return {
        "build_count": build_count,
        "compressed_build_count": len(compressed),
        "compressed_steps": [
            item.get("step")
            for item in compressed
            if item.get("step") is not None
        ],
        "total_build_duration_ms": round(sum(durations), 3),
        "avg_build_duration_ms": (
            round(sum(durations) / build_count, 3) if build_count else 0.0
        ),
        "max_build_duration_ms": round(max(durations), 3) if durations else 0.0,
        "compression_before_chars": before_chars,
        "compression_after_chars": after_chars,
        "compression_saved_chars": saved_chars,
        "compression_ratio": _ratio(after_chars, before_chars, default=1.0),
        "compression_savings_ratio": _ratio(saved_chars, before_chars, default=0.0),
        "max_build_compression_savings_ratio": (
            round(
                max(
                    _float(item.get("compression_savings_ratio"))
                    for item in compressed
                ),
                4,
            )
            if compressed
            else 0.0
        ),
    }


def _section_items(value: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(value, dict):
        return [
            (str(name), dict(section))
            for name, section in value.items()
            if isinstance(section, dict)
        ]
    if isinstance(value, list):
        items = []
        for index, section in enumerate(value):
            if not isinstance(section, dict):
                continue
            name = str(section.get("name") or index)
            items.append((name, dict(section)))
        return items
    return []


def _list_of_dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _as_dict(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "to_dict"):
        try:
            value = value.to_dict()
        except TypeError:
            return {}
    if isinstance(value, dict):
        return dict(value)
    return getattr(value, "__dict__", {}) or {}


def _ratio(numerator: int | float, denominator: int | float, *, default: float) -> float:
    denominator = _float(denominator)
    if denominator <= 0:
        return default
    return round(_float(numerator) / denominator, 4)


def _round(value: Any) -> float:
    return round(_float(value), 3)


def _int(value: Any) -> int:
    try:
        if value is None:
            return 0
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0
