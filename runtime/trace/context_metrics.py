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
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}

    reduced_sections = []
    before_chars = 0
    after_chars = 0
    saved_chars = 0
    token_reduced_sections = []
    before_tokens = 0
    after_tokens = 0
    saved_tokens = 0
    for reduction in reductions:
        before = _int(reduction.get("before_chars"))
        after = _int(reduction.get("after_chars"))
        section = str(reduction.get("section") or "")
        strategy = str(reduction.get("strategy") or "")
        if before > 0 or after > 0:
            saved = max(0, before - after)
            before_chars += before
            after_chars += after
            saved_chars += saved
            reduced_sections.append({
                "section": section,
                "before_chars": before,
                "after_chars": after,
                "saved_chars": saved,
                "compression_ratio": _ratio(after, before, default=1.0),
                "savings_ratio": _ratio(saved, before, default=0.0),
                "strategy": strategy,
            })

        token_before = _int(reduction.get("before_tokens"))
        token_after = _int(reduction.get("after_tokens"))
        if token_before <= 0 and token_after <= 0:
            continue
        token_saved = max(0, token_before - token_after)
        before_tokens += token_before
        after_tokens += token_after
        saved_tokens += token_saved
        token_reduced_sections.append({
            "section": section,
            "before_tokens": token_before,
            "after_tokens": token_after,
            "saved_tokens": token_saved,
            "compression_ratio": _ratio(token_after, token_before, default=1.0),
            "savings_ratio": _ratio(token_saved, token_before, default=0.0),
            "strategy": strategy,
            "generation": _int(reduction.get("generation")),
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

    coding_context = _coding_context_metrics(metadata, sections)
    if before_tokens <= 0 and coding_context["before_tokens"] > 0:
        before_tokens = coding_context["before_tokens"]
        after_tokens = coding_context["after_tokens"]
        saved_tokens = coding_context["saved_tokens"]

    compressed = saved_chars > 0 or saved_tokens > 0
    return {
        "duration_ms": _round(duration_ms),
        "compressed": compressed,
        "compression_before_chars": before_chars,
        "compression_after_chars": after_chars,
        "compression_saved_chars": saved_chars,
        "compression_ratio": _ratio(after_chars, before_chars, default=1.0),
        "compression_savings_ratio": _ratio(saved_chars, before_chars, default=0.0),
        "compression_before_tokens": before_tokens,
        "compression_after_tokens": after_tokens,
        "compression_saved_tokens": saved_tokens,
        "token_compression_ratio": _ratio(after_tokens, before_tokens, default=1.0),
        "token_compression_savings_ratio": _ratio(saved_tokens, before_tokens, default=0.0),
        "reduction_count": len(reductions),
        "reduced_sections": reduced_sections,
        "token_reduced_sections": token_reduced_sections,
        "truncated_sections": truncated_sections,
        "section_raw_chars": section_raw_chars,
        "section_rendered_chars": section_rendered_chars,
        "section_render_ratio": _ratio(section_rendered_chars, section_raw_chars, default=1.0),
        "final_total_chars": _int(payload.get("total_chars")),
        "budget_chars": payload.get("budget_chars"),
        "over_budget": bool(payload.get("over_budget")),
        "coding_context": coding_context,
        "coding_context_enabled": coding_context["enabled"],
        "coding_context_compacted": coding_context["compacted"],
        "coding_context_generation": coding_context["generation"],
        "coding_context_prompt_tail_start_index": coding_context["prompt_tail_start_index"],
        "coding_context_compacted_until_index": coding_context["compacted_until_index"],
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
    token_compressed = [
        item for item in records if _int(item.get("compression_saved_tokens")) > 0
    ]
    before_tokens = sum(_int(item.get("compression_before_tokens")) for item in token_compressed)
    after_tokens = sum(_int(item.get("compression_after_tokens")) for item in token_compressed)
    saved_tokens = sum(_int(item.get("compression_saved_tokens")) for item in token_compressed)
    coding_records = [item for item in records if item.get("coding_context_enabled")]
    coding_compacted = [item for item in coding_records if item.get("coding_context_compacted")]
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
        "token_compressed_build_count": len(token_compressed),
        "token_compressed_steps": [
            item.get("step")
            for item in token_compressed
            if item.get("step") is not None
        ],
        "compression_before_tokens": before_tokens,
        "compression_after_tokens": after_tokens,
        "compression_saved_tokens": saved_tokens,
        "token_compression_ratio": _ratio(after_tokens, before_tokens, default=1.0),
        "token_compression_savings_ratio": _ratio(saved_tokens, before_tokens, default=0.0),
        "coding_context_build_count": len(coding_records),
        "coding_context_compacted_count": len(coding_compacted),
        "coding_context_compacted_steps": [
            item.get("step")
            for item in coding_compacted
            if item.get("step") is not None
        ],
        "coding_context_latest_generation": (
            _int(coding_records[-1].get("coding_context_generation"))
            if coding_records
            else 0
        ),
        "coding_context_max_generation": (
            max(_int(item.get("coding_context_generation")) for item in coding_records)
            if coding_records
            else 0
        ),
        "coding_context_latest_prompt_tail_start_index": (
            _int(coding_records[-1].get("coding_context_prompt_tail_start_index"))
            if coding_records
            else 0
        ),
        "coding_context_latest_compacted_until_index": (
            _int(coding_records[-1].get("coding_context_compacted_until_index"))
            if coding_records
            else 0
        ),
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


def _coding_context_metrics(
    report_metadata: dict[str, Any],
    sections: list[tuple[str, dict[str, Any]]],
) -> dict[str, Any]:
    section_metadata: dict[str, Any] = {}
    for name, section in sections:
        if name != "coding_context":
            continue
        raw_metadata = section.get("metadata")
        if isinstance(raw_metadata, dict):
            section_metadata = dict(raw_metadata)
        break

    enabled = bool(
        report_metadata.get("coding_context_enabled")
        or section_metadata
    )
    before_tokens = _int(
        report_metadata.get("coding_context_before_tokens")
        or section_metadata.get("before_tokens")
    )
    after_tokens = _int(
        report_metadata.get("coding_context_after_tokens")
        or section_metadata.get("after_tokens")
    )
    saved_tokens = max(0, before_tokens - after_tokens)
    return {
        "enabled": enabled,
        "compacted": bool(section_metadata.get("compacted") or saved_tokens > 0),
        "generation": _int(
            report_metadata.get("coding_context_generation")
            or section_metadata.get("generation")
        ),
        "before_tokens": before_tokens,
        "after_tokens": after_tokens,
        "saved_tokens": saved_tokens,
        "compression_ratio": _ratio(after_tokens, before_tokens, default=1.0),
        "savings_ratio": _ratio(saved_tokens, before_tokens, default=0.0),
        "threshold_tokens": _int(section_metadata.get("threshold_tokens")),
        "target_tokens": _int(section_metadata.get("target_tokens")),
        "keep_recent_groups": _int(section_metadata.get("keep_recent_groups")),
        "prompt_tail_start_index": _int(
            report_metadata.get("coding_context_prompt_tail_start_index")
            or section_metadata.get("prompt_tail_start_index")
        ),
        "compacted_until_index": _int(
            report_metadata.get("coding_context_compacted_until_index")
            or section_metadata.get("compacted_until_index")
        ),
        "recent_message_count": _int(section_metadata.get("recent_message_count")),
        "active_message_count": _int(section_metadata.get("active_message_count")),
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
