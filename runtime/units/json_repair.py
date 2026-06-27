from __future__ import annotations

import ast
from dataclasses import dataclass
import json
import re
from typing import Any


@dataclass(frozen=True)
class JsonRepairResult:
    ok: bool
    payload: dict[str, Any] | None = None
    text: str = ""
    repaired: bool = False
    error: str = ""


def repair_json_object(text: str) -> JsonRepairResult:
    """Extract a JSON object from model text without inventing missing fields."""
    raw = str(text or "").lstrip("\ufeff").strip()
    if not raw:
        return JsonRepairResult(ok=False, text="", error="empty_json_text")

    candidates = _candidate_json_objects(raw)
    seen: set[str] = set()
    for candidate, repaired in candidates:
        candidate = candidate.strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        loaded = _load_object(candidate)
        if loaded is not None:
            payload, loaded_text, repair_applied = loaded
            return JsonRepairResult(
                ok=True,
                payload=payload,
                text=loaded_text,
                repaired=repaired or repair_applied or loaded_text != raw,
            )
    return JsonRepairResult(ok=False, text=raw, error="invalid_json_object")


def _candidate_json_objects(text: str) -> list[tuple[str, bool]]:
    candidates: list[tuple[str, bool]] = [(text, False)]
    fenced = re.findall(r"```(?:json)?\s*(.*?)\s*```", text, flags=re.DOTALL | re.IGNORECASE)
    candidates.extend((item, True) for item in fenced)
    balanced = _first_balanced_object(text)
    if balanced:
        candidates.append((balanced, True))
    return candidates


def _load_object(candidate: str) -> tuple[dict[str, Any], str, bool] | None:
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        payload = None
    if isinstance(payload, dict):
        return payload, candidate, False

    for repaired in _repair_candidates(candidate):
        if repaired == candidate:
            continue
        try:
            payload = json.loads(repaired)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload, repaired, True

    try:
        literal = ast.literal_eval(candidate)
    except (SyntaxError, ValueError):
        return None
    if not isinstance(literal, dict):
        return None
    normalized = json.dumps(literal, ensure_ascii=False, separators=(",", ":"))
    return literal, normalized, True


def _repair_candidates(candidate: str) -> list[str]:
    stripped = candidate.strip()
    without_trailing_commas = re.sub(r",\s*([}\]])", r"\1", stripped)
    normalized_quotes = (
        without_trailing_commas
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
    )
    return [without_trailing_commas, normalized_quotes]


def _first_balanced_object(text: str) -> str:
    start = text.find("{")
    if start < 0:
        return ""

    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start:index + 1]
    return ""
