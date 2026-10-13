"""Host-maintained validity of historical observations against current files.

Content hashes describe exact observed bytes, never model-supplied verification.
A stale observation remains readable history, but cannot support a fresh claim.
"""
from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import stat

from .task_state import EvidenceRef, ItemStatus, TaskState, _now


FILE_ABSENT_VERSION = "host:file-absent"


def _observed_path_version(root: Path, path: str) -> str | None:
    try:
        target = (root / path).resolve()
        if not target.is_relative_to(root):
            return None
        try:
            info = target.stat()
        except FileNotFoundError:
            # Stable deletion is a version too: tests after removing a file
            # remain valid until that observed path reappears.
            return FILE_ABSENT_VERSION
        if not stat.S_ISREG(info.st_mode):
            return None
        return sha256(target.read_bytes()).hexdigest()
    except (OSError, ValueError, RuntimeError):
        return None


def _mark_stale(state: TaskState, evidence: EvidenceRef, reason: str) -> bool:
    if evidence.stale:
        return False
    evidence.stale = True
    evidence.stale_reason = reason
    return True


def refresh_evidence_validity(state: TaskState, workspace_root: Path) -> TaskState:
    """Selectively revalidate observed files, including edits by other agents.

    This is not a whole-workspace fingerprint. Test evidence is conservatively
    invalidated when any previously observed dependency has changed.
    """
    root = Path(workspace_root).resolve()
    updated = deepcopy(state)
    hashes: dict[str, str | None] = {}
    changed = False
    dependency_changed = False
    for evidence in updated.evidence_index.values():
        if evidence.stale or not evidence.file_dependencies:
            continue
        reason = ""
        if evidence.workspace_root and evidence.workspace_root != str(root):
            reason = "observation belongs to a different workspace"
        else:
            for path, expected in evidence.file_dependencies.items():
                if path not in hashes:
                    hashes[path] = _observed_path_version(root, path)
                if hashes[path] != expected:
                    reason = f"file changed or unavailable: {path}; reread and revalidate"
                    break
        if reason:
            changed |= _mark_stale(updated, evidence, reason)
            dependency_changed = True
    if dependency_changed:
        for evidence in updated.evidence_index.values():
            if evidence.verification:
                changed |= _mark_stale(updated, evidence, "workspace dependency changed after observation; rerun verification")
    from .reasoning_memory import propagate_reasoning_invalidations
    changed |= propagate_reasoning_invalidations(updated)
    if changed:
        updated.version += 1
        updated.updated_at = _now()
        return updated
    return state


def invalidate_after_tool_effect(state: TaskState, observation: EvidenceRef, *, propagate: bool = True) -> None:
    """Apply ordered write effects before adding the new event's evidence.

    Applied/partial writes invalidate earlier checks, even when they returned an
    error. Denied/no-effect operations leave prior evidence alone.
    """
    if observation.tool_name not in {"write_file", "edit_file", "apply_patch", "delete_file"}:
        return
    if observation.effect_state not in {"applied", "partial"}:
        return
    path = observation.path
    for previous in state.evidence_index.values():
        if previous.verification or not path or path in previous.file_dependencies or path == previous.path:
            _mark_stale(state, previous, f"workspace modified by {observation.event_id}; revalidate against current files")

    if propagate:
        from .reasoning_memory import propagate_reasoning_invalidations
        propagate_reasoning_invalidations(state)


def capture_observed_file_versions(session, *, paths=None) -> dict:
    """Hash only previously observed paths at a host tool-execution boundary.

    Historical hashes select dependencies, never supply their current versions.
    Unobserved files are outside this bounded check; this is not a workspace scan.
    """
    from runtime.workspace import workspace_root_for_session
    from .task_state import load_task_state

    root = workspace_root_for_session(session).resolve()
    if paths is None:
        known_paths = set()
        state = load_task_state(session) if session is not None else None
        if state is not None:
            for evidence in state.evidence_index.values():
                # Include stale reads: a fresh verification must bind the new
                # version even when no second source read was necessary.
                known_paths.update(evidence.file_dependencies)
        # Tool observations newer than the last state compaction are still
        # present in the raw transport. Never parse model text as observations.
        for message in getattr(session, "messages", ()):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            for observation in metadata.get("file_observations") or []:
                if isinstance(observation, dict) and isinstance(observation.get("path"), str):
                    known_paths.add(observation["path"])
    else:
        known_paths = set(paths)
    versions = {path: _observed_path_version(root, path)
                for path in sorted(value for value in known_paths if isinstance(value, str) and value)}
    return {"workspace_root": str(root), "files": versions}


def verification_file_observation(before: dict, after: dict) -> dict:
    """Record stable boundary versions, or require a rerun if bytes moved."""
    initial, final = before["files"], after["files"]
    invalid = [path for path in initial
               if initial[path] is None or final.get(path) is None or initial[path] != final.get(path)]
    if before["workspace_root"] != after["workspace_root"]:
        invalid = list(initial) or ["workspace changed"]
    return {"scope": "previously_observed_files", "workspace_root": after["workspace_root"],
            "file_dependencies": {path: version for path, version in final.items() if version is not None},
            "invalid_paths": invalid}
