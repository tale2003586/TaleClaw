from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
from typing import Any

from config import SUBAGENT_MAX_SCOPE_FILES
from runtime.failure_reasons import SubagentFailureReason
from runtime.workspace import safe_workspace_path, workspace_root_for_session


@dataclass(frozen=True)
class ScopeValidationFailure:
    reason: str
    message: str
    retry_hint: str
    state: dict[str, Any] = field(default_factory=dict)


_PROMPT_PATH_RE = re.compile(
    r"(?<![\w./-])"
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_]+"
    r"|(?<![\w./-])[A-Za-z0-9_.-]+\."
    r"(?:py|js|jsx|ts|tsx|mjs|cjs|json|md|toml|yaml|yml|html|css|scss|txt|sh)"
    r"(?![\w./-])"
)


def validate_subagent_task_scopes(
    session,
    tasks: list[dict[str, Any]],
    *,
    tool_name: str,
    max_files: int | None = None,
) -> ScopeValidationFailure | None:
    limit = max(1, int(max_files or SUBAGENT_MAX_SCOPE_FILES))
    for index, task in enumerate(tasks or []):
        files = _scope_files(task.get("scope"))
        prompt_paths = _extract_prompt_paths(task)
        if not files:
            failure = _missing_scope_files_failure(
                session,
                prompt_paths,
                task_index=index,
                tool_name=tool_name,
                limit=limit,
            )
            if failure is not None:
                return failure
            continue
        failure = _validate_scope_files(
            session,
            files,
            task_index=index,
            tool_name=tool_name,
            limit=limit,
        )
        if failure is not None:
            return failure
    return None


def _missing_scope_files_failure(
    session,
    prompt_paths: list[str],
    *,
    task_index: int,
    tool_name: str,
    limit: int,
) -> ScopeValidationFailure:
    prompt_diagnostics = _path_diagnostics(session, prompt_paths)
    return ScopeValidationFailure(
        reason=SubagentFailureReason.SCOPE_TOO_BROAD.value,
        message=(
            f"Subagent task {task_index} has no scope.files. "
            "Short-lived subagents require a verified concrete file list."
        ),
        retry_hint=(
            "Run repo_map/list_files for the target directory, then retry with "
            f"scope.files containing at most {limit} verified relative file paths. "
            "Do not rely on file names embedded only in the prompt."
        ),
        state={
            "dispatch_rejected": True,
            "tool_name": tool_name,
            "task_index": task_index,
            "reason": "missing_scope_files",
            "prompt_paths": prompt_paths,
            "missing_paths": prompt_diagnostics["missing"],
            "invalid_files": prompt_diagnostics["invalid"],
            "suggestions": prompt_diagnostics["suggestions"],
            "verified_prompt_paths": prompt_diagnostics["verified"],
            "max_scope_files": limit,
            "hint": (
                "scope.files is required. Use repo_map/list_files to confirm real "
                "workspace-relative file names before dispatching subagents."
            ),
        },
    )


def _validate_scope_files(
    session,
    files: list[str],
    *,
    task_index: int,
    tool_name: str,
    limit: int,
) -> ScopeValidationFailure | None:
    unique_files = _unique(files)
    if len(unique_files) > limit:
        return ScopeValidationFailure(
            reason=SubagentFailureReason.SCOPE_TOO_BROAD.value,
            message=(
                f"Subagent task {task_index} declares {len(unique_files)} scope files; "
                f"the limit is {limit}."
            ),
            retry_hint=(
                f"Split this clue into smaller tasks with at most {limit} verified "
                "relative files each, or handle the synthesis in the parent agent."
            ),
            state={
                "dispatch_rejected": True,
                "tool_name": tool_name,
                "task_index": task_index,
                "reason": "scope_file_limit_exceeded",
                "max_scope_files": limit,
                "scope_file_count": len(unique_files),
                "scope_files": unique_files,
                "hint": (
                    f"Split this subtask so each scope.files list has at most {limit} "
                    "verified relative file paths."
                ),
            },
        )

    diagnostics = _path_diagnostics(session, unique_files)
    missing = diagnostics["missing"]
    invalid = diagnostics["invalid"]
    directories = diagnostics["directories"]
    verified = diagnostics["verified"]
    suggestions = diagnostics["suggestions"]

    if directories:
        return ScopeValidationFailure(
            reason=SubagentFailureReason.SCOPE_TOO_BROAD.value,
            message=(
                f"Subagent task {task_index} scope.files contains directories, "
                "but short-lived subagents require concrete files."
            ),
            retry_hint=(
                "Use repo_map/list_files in the parent agent to expand the directory, "
                f"then dispatch at most {limit} verified files per subtask."
            ),
            state={
                "dispatch_rejected": True,
                "tool_name": tool_name,
                "task_index": task_index,
                "reason": "directory_scope",
                "directories": directories,
                "directory_paths": directories,
                "verified_files": verified,
                "max_scope_files": limit,
                "hint": (
                    "scope.files must contain concrete files, not directories. "
                    "Expand directories with repo_map/list_files before dispatch."
                ),
            },
        )

    if missing or invalid:
        return ScopeValidationFailure(
            reason=SubagentFailureReason.MISSING_REQUIRED_FILES.value,
            message=(
                f"Subagent task {task_index} references missing or invalid scope files."
            ),
            retry_hint=(
                "Run repo_map/list_files first and retry only with verified relative "
                "file paths from the current workspace."
            ),
            state={
                "dispatch_rejected": True,
                "tool_name": tool_name,
                "task_index": task_index,
                "reason": "missing_or_invalid_scope_files",
                "missing_paths": missing,
                "missing_files": missing,
                "invalid_files": invalid,
                "suggestions": suggestions,
                "verified_files": verified,
                "max_scope_files": limit,
                "hint": (
                    "These paths do not exist in the current workspace. Use "
                    "repo_map/list_files to confirm real file names, paying attention "
                    "to extensions such as .tsx/.mjs/.json and nonstandard test directories."
                ),
            },
        )

    return None


def _scope_files(scope: Any) -> list[str]:
    if not isinstance(scope, dict):
        return []
    files = scope.get("files")
    if not isinstance(files, list):
        return []
    return [str(item).strip() for item in files if str(item or "").strip()]


def _extract_prompt_paths(task: dict[str, Any]) -> list[str]:
    text = "\n".join(
        str(task.get(key) or "")
        for key in ("prompt", "description", "objective", "deliverable")
    )
    return _unique(match.group(0).strip("`'\"，。；;:()[]{}<>") for match in _PROMPT_PATH_RE.finditer(text))


def _path_diagnostics(session, paths: list[str]) -> dict[str, Any]:
    missing: list[str] = []
    invalid: list[dict[str, str]] = []
    directories: list[str] = []
    verified: list[str] = []
    suggestions: dict[str, list[str]] = {}
    root = _workspace_root(session)
    for file_path in _unique(paths):
        try:
            resolved = safe_workspace_path(file_path, session=session)
        except Exception as exc:
            invalid.append({"path": file_path, "error": f"{type(exc).__name__}: {exc}"})
            continue
        if not resolved.exists():
            display = _display_path(file_path, resolved, root)
            missing.append(display)
            similar = _similar_existing_files(resolved, root)
            if similar:
                suggestions[display] = similar
            continue
        if resolved.is_dir():
            directories.append(_display_path(file_path, resolved, root))
            continue
        if not resolved.is_file():
            invalid.append({"path": file_path, "error": "Path is not a regular file."})
            continue
        verified.append(_display_path(file_path, resolved, root))
    return {
        "missing": missing,
        "invalid": invalid,
        "directories": directories,
        "verified": verified,
        "suggestions": suggestions,
    }


def _unique(files: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for file_path in files:
        if file_path in seen:
            continue
        seen.add(file_path)
        unique.append(file_path)
    return unique


def _workspace_root(session) -> Path | None:
    try:
        return workspace_root_for_session(session).resolve()
    except Exception:
        return None


def _display_path(original: str, resolved: Path, root: Path | None) -> str:
    if root is not None:
        try:
            if resolved == root:
                return "."
            if resolved.is_relative_to(root):
                return resolved.relative_to(root).as_posix()
        except Exception:
            pass
    return original


def _similar_existing_files(resolved: Path, root: Path | None) -> list[str]:
    parent = resolved.parent
    if not parent.exists() or not parent.is_dir():
        return []
    stem = resolved.stem
    suggestions: list[str] = []
    try:
        children = sorted(parent.iterdir(), key=lambda item: item.name)
    except OSError:
        return []
    for child in children:
        if not child.is_file() or child.stem != stem:
            continue
        display = _display_path(child.name, child, root)
        if display not in suggestions:
            suggestions.append(display)
        if len(suggestions) >= 5:
            break
    return suggestions
