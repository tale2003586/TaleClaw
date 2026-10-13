"""Bounded recovery of immutable evidence from the caller's own session.

References are identifiers, never authority to load another session. Recovered
messages remain tool data and carry their original event provenance.
"""
from __future__ import annotations

import json
from hashlib import sha256
import re
from typing import Any, Mapping

from runtime.context.events import canonical_json


def read_session_evidence(
    reference: str, *, session: Any, offset: int = 0, limit: int | None = None,
    query: str | None = None, max_results: int = 20,
    start_seq: int | None = None, end_seq: int | None = None,
) -> tuple[str, dict[str, Any]]:
    if session is None or not getattr(session, "id", ""):
        raise ValueError("Session evidence requires an active caller session.")
    if reference.startswith("event://"):
        # General runtime tool results still append to the legacy transport.
        # Synchronize them just as Session.events_after does before searching.
        backfill = getattr(session, "_backfill_legacy_messages", None)
        if callable(backfill):
            backfill()
    if reference == "event://session":
        if not query:
            raise ValueError("event://session requires a non-empty search query.")
        lower = max(1, int(start_seq or 1))
        upper = int(end_seq) if end_seq is not None else None
        if upper is not None and upper < lower:
            raise ValueError("end_seq must be greater than or equal to start_seq.")
        matches = []
        bounded_results = max(1, min(int(max_results or 20), 50))
        for event in getattr(session, "event_log", ()):
            if event.session_id != session.id or event.seq < lower:
                continue
            if upper is not None and event.seq > upper:
                continue
            text = canonical_json(event.payload)
            found = _search(text, str(query), 1)
            if found:
                matches.append({
                    "event_ref": f"event://{event.event_id}", "seq": event.seq,
                    "event_type": event.type, **found[0],
                })
            if len(matches) >= bounded_results:
                break
        payload = {
            "artifact_ref": reference, "session_id": session.id,
            "instructions": False, "query": query, "start_seq": lower,
            "end_seq": upper, "matches": matches, "match_count": len(matches),
            "results_limited_to": bounded_results,
        }
        # The session index grows: never deduplicate a search as immutable.
        return json.dumps(payload, ensure_ascii=False), {}
    if start_seq is not None or end_seq is not None:
        raise ValueError("Sequence filters are only supported for event://session searches.")
    provenance: dict[str, Any] = {"instructions": False, "session_id": session.id}
    mutable = reference == "state://current"
    if reference.startswith("state://"):
        if not mutable:
            raise ValueError("Only state://current is supported; state is scoped to the caller session.")
        metadata = getattr(session, "metadata", {})
        state = metadata.get("task_state") if isinstance(metadata, Mapping) else None
        if not isinstance(state, Mapping):
            raise ValueError("No task state exists in the current session.")
        text = canonical_json(state)
        core = state.get("core") if isinstance(state.get("core"), Mapping) else state
        provenance.update(
            origin="runtime_task_state", mutable=True,
            version=core.get("version"), updated_at=core.get("updated_at"),
            state_sha256=sha256(text.encode("utf-8")).hexdigest(),
            pagination="Restart from offset 0 if version or state_sha256 changes between pages.",
        )
    elif reference.startswith("event://"):
        event_id = reference.removeprefix("event://")
        event = next((item for item in getattr(session, "event_log", ())
                      if item.event_id == event_id and item.session_id == session.id), None)
        if event is None:
            resolved_id = _branch_alias(session, "event_id_map", event_id)
            event = next((item for item in getattr(session, "event_log", ())
                          if item.event_id == resolved_id and item.session_id == session.id), None)
            if event is not None:
                provenance["resolved_artifact_ref"] = f"event://{event.event_id}"
        if event is None:
            raise ValueError(f"Event not found in the current session: {reference}")
        text = canonical_json(event.to_dict())
        provenance.update(event_type=event.type, seq=event.seq)
    elif reference.startswith("snapshot://"):
        from runtime.context.snapshots import ContextSnapshot

        snapshot_id = reference.removeprefix("snapshot://")
        snapshots = [item.to_dict() if isinstance(item, ContextSnapshot) else item
                     for item in getattr(session, "context_snapshots", ())]
        snapshot = next((item for item in snapshots
                         if isinstance(item, Mapping) and item.get("snapshot_id") == snapshot_id
                         and item.get("session_id") == session.id), None)
        if snapshot is None:
            resolved_id = _branch_alias(session, "snapshot_id_map", snapshot_id)
            snapshot = next((item for item in snapshots
                             if isinstance(item, Mapping) and item.get("snapshot_id") == resolved_id
                             and item.get("session_id") == session.id), None)
            if snapshot is not None:
                snapshot_id = resolved_id
                provenance["resolved_artifact_ref"] = f"snapshot://{snapshot_id}"
        if snapshot is None:
            raise ValueError(f"Snapshot not found in the current session: {reference}")
        text = canonical_json(snapshot)
        provenance.update(
            origin="unverified_summary", snapshot_id=snapshot_id,
            generation=snapshot.get("generation"),
            previous_snapshot_ref=(f"snapshot://{snapshot['previous_snapshot_id']}"
                                   if snapshot.get("previous_snapshot_id") else None),
            covered_event_start_seq=snapshot.get("covered_event_start_seq"),
            covered_event_end_seq=snapshot.get("covered_event_end_seq"),
        )
    else:
        raise ValueError(f"Unsupported session evidence reference: {reference}")
    base = {"artifact_ref": reference, "name": reference, "mime_type": "application/json",
            "size_chars": len(text), **provenance}
    if query is not None:
        if not str(query):
            raise ValueError("read_artifact query must not be empty.")
        bounded_results = max(1, min(int(max_results or 20), 50))
        matches = _search(text, str(query), bounded_results)
        output = json.dumps({**base, "query": query, "matches": matches,
                             "match_count": len(matches), "results_limited_to": bounded_results},
                            ensure_ascii=False)
        return output, {} if mutable else {"artifact_ref": reference, "mode": "search", "query": str(query),
                        "max_results": bounded_results, "size_chars": len(text)}
    start = max(0, int(offset or 0))
    if start > len(text):
        raise ValueError(f"read_artifact offset {start} exceeds evidence size {len(text)}.")
    bounded_limit = max(200, min(12_000 if limit is None else int(limit), 50_000))
    end = min(len(text), start + bounded_limit)
    header = {**base, "offset": start, "returned_chars": end - start,
              "truncated": end < len(text), "next_offset": end if end < len(text) else None}
    return json.dumps(header, ensure_ascii=False) + "\n\n" + text[start:end], {} if mutable else {
        "artifact_ref": reference, "mode": "range", "returned_range": [start, end],
        "size_chars": len(text), "truncated": end < len(text),
        "next_offset": header["next_offset"], "eof": end == len(text),
    }


def _branch_alias(session, field_name: str, identifier: str) -> str | None:
    # This translates an identifier only. Callers must still find the target in
    # their own event/snapshot collection and verify its session_id; lineage is
    # never authority to open the ancestor session or an external store.
    metadata = getattr(session, "metadata", {})
    lineage = metadata.get("branch_lineage") if isinstance(metadata, Mapping) else None
    aliases = lineage.get(field_name) if isinstance(lineage, Mapping) else None
    target = aliases.get(identifier) if isinstance(aliases, Mapping) else None
    return target if isinstance(target, str) and target else None


def _search(text: str, query: str, max_results: int) -> list[dict[str, Any]]:
    matches = []
    for match in re.finditer(re.escape(query), text, re.IGNORECASE):
        start, end = match.span()
        # Keep snippets bounded even for unusually long search strings.
        snippet_end = min(len(text), start + 400, end + 120)
        matches.append({"start": start, "end": end,
                        "line": text.count("\n", 0, start) + 1,
                        "snippet": text[max(0, start - 120):snippet_end]})
        if len(matches) >= max_results:
            break
    return matches
