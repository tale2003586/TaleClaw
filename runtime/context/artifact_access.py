"""Deterministic per-session access tracking for immutable artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import hashlib
from typing import Any, Literal, Mapping


RESOURCE_STATE_METADATA_KEY = "resource_state"
ARTIFACT_ACCESS_STATE_KEY = "artifact_access"
ARTIFACT_PROMPT_VISIBILITY_KEY = "artifact_prompt_visibility"
MAX_ACCESS_SUMMARY_RESOURCES = 20
MAX_ACCESS_SUMMARY_RANGES = 8
MAX_ACCESS_SUMMARY_CHARS = 6_000

ArtifactAccessStatus = Literal[
    "not_accessed",
    "partially_accessed",
    "fully_accessed",
    "search_only",
    "failed",
]


@dataclass
class ArtifactAccessState:
    artifact_ref: str
    size_chars: int | None = None
    access_status: ArtifactAccessStatus = "not_accessed"
    covered_ranges: list[tuple[int, int]] = field(default_factory=list)
    eof: bool = False
    normalized_queries: set[str] = field(default_factory=set)
    total_read_calls: int = 0
    repeated_read_calls: int = 0
    no_progress_count: int = 0
    last_offset: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["covered_ranges"] = [list(item) for item in self.covered_ranges]
        data["normalized_queries"] = sorted(self.normalized_queries)
        return data

    @classmethod
    def from_payload(cls, payload: Any) -> "ArtifactAccessState | None":
        if not isinstance(payload, Mapping) or not payload.get("artifact_ref"):
            return None
        try:
            return cls(
                artifact_ref=canonical_artifact_ref(payload.get("artifact_ref")),
                size_chars=(
                    int(payload["size_chars"])
                    if payload.get("size_chars") is not None
                    else None
                ),
                access_status=str(payload.get("access_status") or "not_accessed"),
                covered_ranges=merge_ranges([
                    (int(item[0]), int(item[1]))
                    for item in payload.get("covered_ranges") or []
                    if isinstance(item, (list, tuple)) and len(item) == 2
                ]),
                eof=bool(payload.get("eof")),
                normalized_queries={
                    str(item) for item in payload.get("normalized_queries") or [] if item
                },
                total_read_calls=max(0, int(payload.get("total_read_calls") or 0)),
                repeated_read_calls=max(0, int(payload.get("repeated_read_calls") or 0)),
                no_progress_count=max(0, int(payload.get("no_progress_count") or 0)),
                last_offset=(
                    int(payload["last_offset"])
                    if payload.get("last_offset") is not None
                    else None
                ),
                last_error=(str(payload.get("last_error")) if payload.get("last_error") else None),
            )
        except (TypeError, ValueError):
            return None


def load_artifact_access_states(metadata: Mapping[str, Any] | None) -> dict[str, ArtifactAccessState]:
    resources = metadata.get(RESOURCE_STATE_METADATA_KEY) if isinstance(metadata, Mapping) else None
    raw = resources.get(ARTIFACT_ACCESS_STATE_KEY) if isinstance(resources, Mapping) else None
    if not isinstance(raw, Mapping):
        return {}
    states: dict[str, ArtifactAccessState] = {}
    for value in raw.values():
        state = ArtifactAccessState.from_payload(value)
        if state is not None:
            states[state.artifact_ref] = state
    return states


def save_artifact_access_state(metadata: dict[str, Any], state: ArtifactAccessState) -> None:
    resources = metadata.get(RESOURCE_STATE_METADATA_KEY)
    if not isinstance(resources, dict):
        resources = {}
        metadata[RESOURCE_STATE_METADATA_KEY] = resources
    states = resources.get(ARTIFACT_ACCESS_STATE_KEY)
    if not isinstance(states, dict):
        states = {}
        resources[ARTIFACT_ACCESS_STATE_KEY] = states
    states.pop(state.artifact_ref, None)
    states[state.artifact_ref] = state.to_dict()


def register_artifact_access_state(
    metadata: dict[str, Any],
    artifact_ref: Any,
    *,
    size_chars: int | None = None,
) -> ArtifactAccessState:
    if isinstance(artifact_ref, Mapping):
        raw_size = artifact_ref.get("size_chars")
        if size_chars is None and raw_size is not None:
            size_chars = int(raw_size)
        artifact_ref = artifact_ref.get("storage_uri") or artifact_ref.get("artifact_id")
    reference = canonical_artifact_ref(artifact_ref)
    state = load_artifact_access_states(metadata).get(reference)
    if state is None:
        state = ArtifactAccessState(
            artifact_ref=reference,
            size_chars=(int(size_chars) if size_chars is not None else None),
        )
        save_artifact_access_state(metadata, state)
    return state


def guard_artifact_access(
    metadata: dict[str, Any],
    arguments: Mapping[str, Any],
) -> str | None:
    reference = canonical_artifact_ref(arguments.get("artifact_ref"))
    state = load_artifact_access_states(metadata).get(reference)
    if state is None:
        return None
    visible = _visible_access(metadata)
    if visible is not None:
        entry = visible.get(reference) or {}
        if arguments.get("query") is not None:
            duplicate = _search_key(arguments) in entry.get("queries", [])
        else:
            start = max(0, int(arguments.get("offset") or 0))
            end = start + max(200, min(int(arguments.get("limit") or 12_000), 50_000))
            if state.size_chars is not None:
                end = min(end, state.size_chars)
            duplicate = end >= start and range_is_covered(entry.get("ranges", []), start, end)
            if end == start:
                duplicate = bool(entry.get("eof"))
        if duplicate:
            count = _record_repeated_attempt(metadata, state)
            return _duplicate_message("currently visible evidence", count=count)
        return None
    query = arguments.get("query")
    if query is not None:
        normalized = normalize_query(query)
        if normalized and normalized in state.normalized_queries:
            count = _record_repeated_attempt(metadata, state)
            return _duplicate_message("search query", count=count)
        return None
    start = max(0, int(arguments.get("offset") or 0))
    limit = arguments.get("limit")
    bounded_limit = max(200, min(int(limit or 12_000), 50_000))
    end = start + bounded_limit
    if state.size_chars is not None:
        end = min(end, state.size_chars)
    if end <= start or range_is_covered(state.covered_ranges, start, end):
        count = _record_repeated_attempt(metadata, state)
        return _duplicate_message("artifact range", count=count)
    return None


def reduce_artifact_access(
    metadata: dict[str, Any],
    access: dict[str, Any],
) -> ArtifactAccessState:
    reference = canonical_artifact_ref(access.get("artifact_ref"))
    state = load_artifact_access_states(metadata).get(reference) or ArtifactAccessState(artifact_ref=reference)
    state.total_read_calls += 1
    size_chars = access.get("size_chars")
    if size_chars is not None:
        state.size_chars = max(0, int(size_chars))
    mode = str(access.get("mode") or "range")
    new_coverage = 0
    if mode == "search":
        normalized = normalize_query(access.get("normalized_query"))
        if normalized:
            if normalized in state.normalized_queries:
                state.repeated_read_calls += 1
                state.no_progress_count += 1
            else:
                state.normalized_queries.add(normalized)
                state.no_progress_count = 0
        if not state.covered_ranges:
            state.access_status = "search_only"
    else:
        returned = access.get("returned_range")
        if isinstance(returned, (list, tuple)) and len(returned) == 2:
            start, end = max(0, int(returned[0])), max(0, int(returned[1]))
            before = covered_chars(state.covered_ranges)
            state.covered_ranges = merge_ranges([*state.covered_ranges, (start, end)])
            new_coverage = max(0, covered_chars(state.covered_ranges) - before)
            state.last_offset = start
        state.eof = bool(access.get("eof")) or state.eof
        if new_coverage:
            state.no_progress_count = 0
        else:
            state.repeated_read_calls += 1
            state.no_progress_count += 1
        fully_covered = bool(
            state.size_chars is not None
            and range_is_covered(state.covered_ranges, 0, state.size_chars)
        )
        state.access_status = "fully_accessed" if fully_covered else "partially_accessed"
    if _visible_access(metadata) is not None:
        # A successful repeated read restores evidence lost from the prompt;
        # historical coverage alone is not a no-progress signal.
        access["rehydrated"] = state.no_progress_count > 0
        state.no_progress_count = 0
        _add_visible_access(_visible_access(metadata), access)
    state.last_error = None
    access["new_coverage_chars"] = new_coverage
    save_artifact_access_state(metadata, state)
    return state


def record_artifact_access_failure(
    metadata: dict[str, Any],
    artifact_ref: Any,
    error: str,
) -> ArtifactAccessState:
    reference = canonical_artifact_ref(artifact_ref)
    state = load_artifact_access_states(metadata).get(reference) or ArtifactAccessState(reference)
    state.total_read_calls += 1
    state.no_progress_count += 1
    state.last_error = str(error or "artifact read failed")[:500]
    if not state.covered_ranges and not state.normalized_queries:
        state.access_status = "failed"
    save_artifact_access_state(metadata, state)
    return state


def artifact_access_summary_message(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    states = load_artifact_access_states(metadata)
    if not states:
        return None
    visible = _visible_access(metadata or {}) or {}
    # Access history stays durable. Only its prompt projection is bounded.
    ordered = list(reversed(list(states.values())))
    ordered.sort(key=lambda state: state.artifact_ref not in visible)
    resources = []
    for state in ordered:
        candidate = {
            "artifact_ref": state.artifact_ref,
            "access_status": state.access_status,
            "covered_ranges": [list(item) for item in state.covered_ranges[-MAX_ACCESS_SUMMARY_RANGES:]],
            "omitted_ranges": max(0, len(state.covered_ranges) - MAX_ACCESS_SUMMARY_RANGES),
            "eof": state.eof,
            "search_count": len(state.normalized_queries),
            "repeated_read_calls": state.repeated_read_calls,
        }
        if len(json.dumps([*resources, candidate], ensure_ascii=False, separators=(",", ":"))) > MAX_ACCESS_SUMMARY_CHARS - 500:
            continue
        resources.append(candidate)
        if len(resources) >= MAX_ACCESS_SUMMARY_RESOURCES:
            break
    payload = {
        "coverage_scope": "historical; reread if absent from current prompt",
        "tracked_resources": len(states),
        "omitted_resources": len(states) - len(resources),
        "resources": resources,
    }
    return {
        "role": "system",
        "content": (
            '<artifact-access-state source="runtime-generated" instructions="false">\n'
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + "\n</artifact-access-state>"
        ),
        "metadata": {
            "kind": "artifact_access_state",
            "source": "runtime-generated",
            "instructions": False,
        },
    }


def merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    normalized = sorted((max(0, start), max(0, end)) for start, end in ranges if end > start)
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def covered_chars(ranges: list[tuple[int, int]]) -> int:
    return sum(end - start for start, end in merge_ranges(ranges))


def range_is_covered(ranges: list[tuple[int, int]], start: int, end: int) -> bool:
    if end <= start:
        return True
    return any(left <= start and right >= end for left, right in merge_ranges(ranges))


def canonical_artifact_ref(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text if text.startswith(("artifact://", "event://", "snapshot://", "state://")) else f"artifact://{text}"


def normalize_query(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _record_repeated_attempt(metadata: dict[str, Any], state: ArtifactAccessState) -> int:
    state.repeated_read_calls += 1
    state.no_progress_count += 1
    save_artifact_access_state(metadata, state)
    return state.no_progress_count


def _duplicate_message(kind: str, *, count: int) -> str:
    return (
        f"[artifact-access-guard no_progress_count={count}] "
        f"The requested {kind} has already been covered. "
        "No new information would be produced by this call. "
        "Continue using the existing artifact evidence or choose a different query."
    )


def evidence_response_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def update_artifact_prompt_visibility(
    metadata: dict[str, Any], messages: list[dict[str, Any]],
) -> None:
    """Track full evidence in the actual prompt, independently of read history.

    Metadata survives some truncation paths. Require the response digest to
    match as well, so a retained header/preview never masquerades as full data.
    """
    resources = metadata.setdefault(RESOURCE_STATE_METADATA_KEY, {})
    if not isinstance(resources, dict):
        resources = {}
        metadata[RESOURCE_STATE_METADATA_KEY] = resources
    visible: dict[str, Any] = {}
    for message in messages:
        if message.get("role") != "tool":
            continue
        message_metadata = message.get("metadata")
        access = message_metadata.get("artifact_access") if isinstance(message_metadata, Mapping) else None
        content = message.get("content")
        if (isinstance(access, Mapping) and isinstance(content, str)
                and access.get("response_sha256") == evidence_response_digest(content)):
            _add_visible_access(visible, access)
    resources[ARTIFACT_PROMPT_VISIBILITY_KEY] = visible


def _visible_access(metadata: Mapping[str, Any]) -> dict[str, Any] | None:
    resources = metadata.get(RESOURCE_STATE_METADATA_KEY)
    value = resources.get(ARTIFACT_PROMPT_VISIBILITY_KEY) if isinstance(resources, Mapping) else None
    return value if isinstance(value, dict) else None


def _search_key(arguments: Mapping[str, Any]) -> str:
    # Artifact search is case insensitive but whitespace is significant.
    return json.dumps([str(arguments.get("query") or "").casefold(),
                       max(1, min(int(arguments.get("max_results") or 20), 50))],
                      ensure_ascii=False)


def _add_visible_access(visible: dict[str, Any], access: Mapping[str, Any]) -> None:
    reference = canonical_artifact_ref(access.get("artifact_ref"))
    if not reference or reference == "event://session":
        return
    entry = visible.setdefault(reference, {"ranges": [], "queries": [], "eof": False})
    if access.get("mode") == "search":
        key = _search_key(access)
        if key not in entry["queries"]:
            entry["queries"].append(key)
    else:
        returned = access.get("returned_range")
        if isinstance(returned, (tuple, list)) and len(returned) == 2:
            entry["ranges"] = [list(item) for item in merge_ranges([
                *entry["ranges"], (int(returned[0]), int(returned[1])),
            ])]
        entry["eof"] = bool(entry["eof"] or access.get("eof"))
