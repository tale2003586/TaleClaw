import ast
from collections import Counter, defaultdict
import keyword
import json
import os
import hashlib
import fnmatch
import re
import shutil
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
import subprocess
from uuid import uuid4


from applications.coding.orchestration.background_task import BG
from config import (
    CODE_OUTLINE_LARGE_FILE_LINES,
    CODE_OUTLINE_MAX_CHARS,
    CONTEXT_ARTIFACT_ROOT,
    ORCHESTRATION_REPAIR_ROUNDS,
    REPO_MAP_DEFAULT_MAX_DEPTH,
    REPO_MAP_MAX_CHARS,
    REPO_MAP_MAX_FILE_BYTES,
    WORKDIR,
)
from runtime.context.artifacts import ArtifactNotFoundError, ArtifactStore
from runtime.messaging import AgentMessage, MessageType, render_agent_message
from runtime.messaging.team_bus import BUS
from agents.subagent.orchestration_state import (
    OrchestrationDecision,
    guard_subagent_dispatch,
    record_subagent_dispatch,
    record_subagent_results,
    rejected_parallel_response,
    rejection_response,
    rejection_trace_payload,
)
from runtime.workspace import safe_workspace_path, workspace_root_for_session
from applications.coding.orchestration.protocols import PROTOCOLS
from skill_runtime import SKILL_LOADER
from applications.coding.orchestration.task import TASKS
from user_scope import (
    explicit_user_id_for_session,
    storage_root_for_session,
)


MAX_WORKSPACE_LIST_ENTRIES = 500
MAX_WORKSPACE_READ_CHARS = 50_000
MAX_BATCH_READ_FILES = 8
DEFAULT_BATCH_READ_LIMIT = 200
MAX_BATCH_READ_CHARS = 80_000
MAX_RG_MATCHES = 500
DEFAULT_RG_MATCHES = 100
RG_TIMEOUT_SECONDS = 30
MAX_GREP_MATCHES = 500
DEFAULT_GREP_MATCHES = 100
REPO_MAP_DEFAULT_MAX_SYMBOLS = int(os.getenv("REPO_MAP_MAX_SYMBOLS", "80"))
REPO_MAP_MAX_SYMBOLS = int(os.getenv("REPO_MAP_SYMBOL_CAP", "300"))
REPO_MAP_SOURCE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".mjs",
    ".cjs",
}
REPO_MAP_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_$][A-Za-z0-9_$]{2,}\b")
REPO_MAP_FILE_RE = re.compile(
    r"(?<![\w./-])[\w./-]+\.(?:py|js|jsx|ts|tsx|mjs|cjs)(?![\w./-])"
)
REPO_MAP_IDENTIFIER_STOPWORDS = {
    "and",
    "any",
    "are",
    "args",
    "async",
    "await",
    "bool",
    "class",
    "const",
    "dict",
    "else",
    "export",
    "false",
    "for",
    "from",
    "function",
    "import",
    "int",
    "let",
    "list",
    "none",
    "not",
    "null",
    "object",
    "return",
    "self",
    "str",
    "this",
    "true",
    "type",
    "var",
    "with",
}
REPO_MAP_GENERIC_IDENTIFIERS = {
    "add",
    "build",
    "data",
    "get",
    "item",
    "load",
    "main",
    "name",
    "path",
    "read",
    "run",
    "save",
    "set",
    "text",
    "update",
    "value",
    "write",
}
TOOL_RESULT_CACHE_KEY = "_tool_result_cache"
TOOL_CACHE_STEP_KEY = "_tool_result_cache_step"


def _format_tool_error(exc: Exception) -> str:
    return f"Error: {type(exc).__name__}: {exc}"


def safe_path(p: str, *, session=None) -> Path:
    return safe_workspace_path(p, session=session)


def run_list_files(
    path: str = "",
    recursive: bool = False,
    offset: int = 0,
    *,
    _session=None,
) -> str:
    try:
        root = workspace_root_for_session(_session).resolve()
        target = safe_workspace_path(path or ".", session=_session)
        if not target.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {path}")
        normalized_path = "" if target == root else target.relative_to(root).as_posix()
        offset = _nonnegative_int(offset, default=0)
        cache_key = _tool_cache_key(
            "list_files",
            {
                "path": normalized_path,
                "recursive": bool(recursive),
                "offset": offset,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached
        entries = []
        iterator = target.rglob("*") if recursive else target.iterdir()
        for child in sorted(iterator, key=lambda item: item.relative_to(root).as_posix()):
            rel_parts = child.relative_to(root).parts
            if any(part.startswith(".") for part in rel_parts):
                continue
            if "__pycache__" in rel_parts:
                continue
            entries.append({
                "path": child.relative_to(root).as_posix(),
                "type": "dir" if child.is_dir() else "file",
            })
        total = len(entries)
        page = entries[offset : offset + MAX_WORKSPACE_LIST_ENTRIES]
        next_offset = offset + len(page)
        truncated = next_offset < total
        output = json.dumps({
            "path": normalized_path,
            "recursive": bool(recursive),
            "offset": offset,
            "limit": MAX_WORKSPACE_LIST_ENTRIES,
            "total": total,
            "entries": page,
            "truncated": truncated,
            "next_offset": next_offset if truncated else None,
            "remaining": max(0, total - next_offset),
        }, ensure_ascii=False, indent=2)
        if truncated:
            output += (
                "\n[list_files] showed entries "
                f"{offset}-{next_offset} of {total}. "
                "To continue: "
                f"list_files(path=\"{normalized_path or '.'}\", "
                f"recursive={bool(recursive)}, offset={next_offset}). "
                f"{total - next_offset} entries remain."
            )
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_rg(
    pattern: str,
    path: str = "",
    glob: str | list[str] | None = None,
    max_matches: int | None = None,
    case_sensitive: bool = True,
    literal: bool = False,
    *,
    _session=None,
) -> str:
    try:
        pattern = str(pattern or "")
        if not pattern:
            raise ValueError("rg pattern is required.")
        root = workspace_root_for_session(_session).resolve()
        target = safe_workspace_path(path or ".", session=_session)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {path or '.'}")
        if not (target.is_dir() or target.is_file()):
            raise ValueError(f"Path is not searchable: {path or '.'}")
        normalized_path = "." if target == root else target.relative_to(root).as_posix()
        match_limit = _bounded_positive_int(
            max_matches,
            default=DEFAULT_RG_MATCHES,
            maximum=MAX_RG_MATCHES,
        )
        command = [
            "rg",
            "--line-number",
            "--column",
            "--no-heading",
            "--color",
            "never",
            "--max-filesize",
            "1M",
        ]
        if not _as_bool(case_sensitive, default=True):
            command.append("--ignore-case")
        if _as_bool(literal, default=False):
            command.append("--fixed-strings")
        for item in _rg_globs(glob):
            command.extend(["--glob", item])
        command.extend(["--", pattern, normalized_path or "."])
        result = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            timeout=RG_TIMEOUT_SECONDS,
        )
        stdout = (result.stdout or "").strip()
        stderr = (result.stderr or "").strip()
        if result.returncode == 1:
            return "(no matches)"
        if result.returncode != 0:
            detail = stderr or stdout or f"exit code {result.returncode}"
            return f"Error: rg failed: {detail[:1000]}"
        lines = stdout.splitlines()
        truncated = len(lines) > match_limit
        visible = lines[:match_limit]
        output = "\n".join(visible) if visible else "(no matches)"
        if truncated:
            output += (
                f"\n[rg] returned first {match_limit} of {len(lines)} match lines. "
                "Narrow path/glob/pattern or raise max_matches if needed."
            )
        if stderr:
            output += f"\n[rg stderr] {stderr[:1000]}"
        return output[:MAX_WORKSPACE_READ_CHARS]
    except subprocess.TimeoutExpired:
        return f"Error: rg timed out after {RG_TIMEOUT_SECONDS}s"
    except Exception as e:
        return _format_tool_error(e)


def run_nl(
    path: str,
    offset: int = 0,
    limit: int | None = None,
    number_blank_lines: bool = True,
    width: int | None = None,
    *,
    _session=None,
) -> str:
    try:
        target = safe_path(path, session=_session)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not target.is_file():
            raise IsADirectoryError(f"Path is not a file: {path}")
        root = workspace_root_for_session(_session).resolve()
        normalized_path = target.relative_to(root).as_posix()
        offset = _nonnegative_int(offset, default=0)
        limit = _optional_positive_int(limit)
        line_width = _bounded_positive_int(width, default=6, maximum=12)
        number_blanks = _as_bool(number_blank_lines, default=True)
        cache_key = _tool_cache_key(
            "nl",
            {
                "path": normalized_path,
                "offset": offset,
                "limit": limit,
                "number_blank_lines": number_blanks,
                "width": line_width,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached

        lines = target.read_text(encoding="utf-8").splitlines()
        numbered = _number_lines(
            lines,
            width=line_width,
            number_blank_lines=number_blanks,
        )
        output = _format_line_window(
            numbered,
            path_label=normalized_path,
            tool_name="nl",
            offset=offset,
            limit=limit,
            max_chars=MAX_WORKSPACE_READ_CHARS,
        )
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_grep(
    pattern: str,
    path: str = "",
    glob: str | list[str] | None = None,
    max_matches: int | None = None,
    case_sensitive: bool = True,
    literal: bool = False,
    recursive: bool = True,
    *,
    _session=None,
) -> str:
    try:
        pattern = str(pattern or "")
        if not pattern:
            raise ValueError("grep pattern is required.")
        root = workspace_root_for_session(_session).resolve()
        target = safe_workspace_path(path or ".", session=_session)
        if not target.exists():
            raise FileNotFoundError(f"Path not found: {path or '.'}")
        if not (target.is_dir() or target.is_file()):
            raise ValueError(f"Path is not searchable: {path or '.'}")
        normalized_path = "." if target == root else target.relative_to(root).as_posix()
        match_limit = _bounded_positive_int(
            max_matches,
            default=DEFAULT_GREP_MATCHES,
            maximum=MAX_GREP_MATCHES,
        )
        globs = _rg_globs(glob)
        recurse = _as_bool(recursive, default=True)
        case = _as_bool(case_sensitive, default=True)
        exact = _as_bool(literal, default=False)
        cache_key = _tool_cache_key(
            "grep",
            {
                "pattern": pattern,
                "path": normalized_path,
                "glob": globs,
                "max_matches": match_limit,
                "case_sensitive": case,
                "literal": exact,
                "recursive": recurse,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached

        matcher = _compile_grep_matcher(
            pattern,
            case_sensitive=case,
            literal=exact,
        )
        matches = []
        truncated = False
        for file_path in _grep_files(root, target, recursive=recurse):
            relative = file_path.relative_to(root).as_posix()
            if globs and not _path_matches_globs(relative, globs):
                continue
            try:
                if file_path.stat().st_size > 1_000_000:
                    continue
                text = file_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for line_no, line in enumerate(text.splitlines(), 1):
                if not matcher(line):
                    continue
                if len(matches) >= match_limit:
                    truncated = True
                    break
                matches.append(f"{relative}:{line_no}:{line}")
            if truncated:
                break

        output = "\n".join(matches) if matches else "(no matches)"
        if truncated:
            output += (
                f"\n[grep] returned first {match_limit} match lines. "
                "Narrow path/glob/pattern or raise max_matches if needed."
            )
        output = output[:MAX_WORKSPACE_READ_CHARS]
        _tool_cache_set(_session, cache_key, output)
        return output
    except re.error as e:
        return f"Error: invalid grep regex: {e}"
    except Exception as e:
        return _format_tool_error(e)


def run_read(path: str, limit: int = None, offset: int = 0, *, _session=None) -> str:
    try:
        target = safe_path(path, session=_session)
        root = workspace_root_for_session(_session).resolve()
        normalized_path = target.relative_to(root).as_posix()
        offset = _nonnegative_int(offset, default=0)
        limit = _optional_positive_int(limit)
        cache_key = _tool_cache_key(
            "read_file",
            {
                "path": normalized_path,
                "offset": offset,
                "limit": limit,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached
        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        if _should_guard_subagent_large_read(_session, lines, offset=offset, limit=limit):
            output = _format_large_file_outline_notice(
                lines,
                path_label=normalized_path,
            )
        else:
            output = _format_line_window(
                lines,
                path_label=normalized_path,
                tool_name="read_file",
                offset=offset,
                limit=limit,
                max_chars=MAX_WORKSPACE_READ_CHARS,
            )
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_read_files(files, output_format: str = "text", *, _session=None) -> str:
    try:
        items = _normalize_read_file_batch(files)
        output_format = str(output_format or "text").strip().lower()
        if output_format not in {"text", "json"}:
            output_format = "text"
        cache_key = _tool_cache_key("read_files", {
            "files": items,
            "output_format": output_format,
        })
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached

        results = []
        for item in items:
            path = item["path"]
            offset = _nonnegative_int(item.get("offset"), default=0)
            limit = _optional_positive_int(item.get("limit"))
            if limit is None:
                limit = DEFAULT_BATCH_READ_LIMIT
            content = run_read(path, limit=limit, offset=offset, _session=_session)
            results.append({
                "path": path,
                "offset": offset,
                "limit": limit,
                "output": content,
            })

        if output_format == "json":
            output = json.dumps({"results": results}, ensure_ascii=False, indent=2)
            _tool_cache_set(_session, cache_key, output)
            return output

        parts = [
            (
                f"[read_files] reading {len(items)} file(s). "
                f"Omitted per-file limit defaults to {DEFAULT_BATCH_READ_LIMIT} lines."
            )
        ]
        truncated = False
        for index, result in enumerate(results, 1):
            path = result["path"]
            offset = result["offset"]
            limit = result["limit"]
            content = result["output"]
            section = (
                f"\n===== read_files {index}/{len(items)}: {path} "
                f"offset={offset} limit={limit} =====\n"
                f"{content}"
            )
            current = "\n".join(parts)
            if len(current) + len(section) > MAX_BATCH_READ_CHARS:
                remaining = max(0, MAX_BATCH_READ_CHARS - len(current) - 300)
                if remaining > 0:
                    parts.append(section[:remaining].rstrip())
                parts.append(
                    "\n[read_files] batch output truncated. Re-read remaining files "
                    "or narrower windows with read_file/read_files offset+limit."
                )
                truncated = True
                break
            parts.append(section)

        output = "\n".join(parts)
        if truncated:
            output = output[:MAX_BATCH_READ_CHARS]
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_retrieve_tool_result(
    result_id: str,
    offset: int = 0,
    limit: int | None = None,
    query: str | None = None,
    *,
    _session=None,
) -> str:
    try:
        from runtime.tooling.result_store import retrieve_tool_result

        return retrieve_tool_result(
            result_id,
            offset=offset,
            limit=limit,
            query=query,
        )
    except Exception as e:
        return _format_tool_error(e)


def run_repo_map(
    path: str = "",
    max_depth: int | None = None,
    include_lines: bool = True,
    offset: int = 0,
    max_symbols: int | None = None,
    *,
    _session=None,
) -> str:
    """Return a deterministic repository map with directory aggregates and ranked symbols."""
    try:
        root = workspace_root_for_session(_session).resolve()
        target = safe_workspace_path(path or ".", session=_session)
        if not target.exists():
            raise FileNotFoundError(f"Directory not found: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {path}")
        normalized_path = "." if target == root else target.relative_to(root).as_posix()
        depth = _optional_nonnegative_int(max_depth)
        if depth is None:
            depth = max(0, int(REPO_MAP_DEFAULT_MAX_DEPTH))
        offset = _nonnegative_int(offset, default=0)
        include_lines = bool(include_lines)
        symbol_limit = _bounded_positive_int(
            max_symbols,
            default=REPO_MAP_DEFAULT_MAX_SYMBOLS,
            maximum=REPO_MAP_MAX_SYMBOLS,
        )
        focus = _repo_map_focus_from_session(_session)
        cache_key = _tool_cache_key(
            "repo_map",
            {
                "path": normalized_path,
                "max_depth": depth,
                "include_lines": include_lines,
                "max_symbols": symbol_limit,
                "focus_files": focus["files"],
                "focus_identifiers": focus["identifiers"],
                "offset": offset,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached

        file_paths, source = _repo_file_paths(root, target, normalized_path)
        directories, files, totals = _build_repo_map_payload(
            root=root,
            target=target,
            file_paths=file_paths,
            max_depth=depth,
            include_lines=include_lines,
        )
        ranked_symbols = _build_ranked_symbol_map_payload(
            root=root,
            file_paths=file_paths,
            max_symbols=symbol_limit,
            focus=focus,
        )
        payload = {
            "path": normalized_path,
            "source": source,
            "max_depth": depth,
            "include_lines": include_lines,
            "max_symbols": symbol_limit,
            "offset": offset,
            "repair_round_limit": ORCHESTRATION_REPAIR_ROUNDS,
            "total_files": totals["files"],
            "total_lines": totals["lines"],
            "line_count_skipped": totals["line_count_skipped"],
            "directories": directories,
            "files": files,
            "files_omitted_by_depth": max(0, totals["files"] - len(files)),
            **ranked_symbols,
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        output = _format_repo_map_window(
            rendered.splitlines(),
            path_label=normalized_path,
            max_depth=depth,
            include_lines=include_lines,
            max_symbols=symbol_limit,
            offset=offset,
        )
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_code_outline(
    path: str,
    offset: int = 0,
    limit: int | None = None,
    *,
    _session=None,
) -> str:
    """Return a paginated symbol outline for a single source file."""
    try:
        target = safe_path(path, session=_session)
        if not target.exists():
            raise FileNotFoundError(f"File not found: {path}")
        if not target.is_file():
            raise IsADirectoryError(f"Path is not a file: {path}")
        root = workspace_root_for_session(_session).resolve()
        normalized_path = target.relative_to(root).as_posix()
        offset = _nonnegative_int(offset, default=0)
        limit = _optional_positive_int(limit)
        cache_key = _tool_cache_key(
            "code_outline",
            {
                "path": normalized_path,
                "offset": offset,
                "limit": limit,
            },
        )
        cached = _tool_cache_get(_session, cache_key)
        if cached is not None:
            return cached

        text = target.read_text(encoding="utf-8")
        lines = text.splitlines()
        symbols = _extract_code_symbols(lines, suffix=target.suffix.lower())
        payload = {
            "path": normalized_path,
            "total_lines": len(lines),
            "symbol_count": len(symbols),
            "offset": offset,
            "symbols": symbols,
        }
        rendered = json.dumps(payload, ensure_ascii=False, indent=2)
        output = _format_code_outline_window(
            rendered.splitlines(),
            path_label=normalized_path,
            offset=offset,
            limit=limit,
        )
        _tool_cache_set(_session, cache_key, output)
        return output
    except Exception as e:
        return _format_tool_error(e)
    
def run_write(path: str, content: str, *, _session=None) -> str:
    try:
        fp = safe_path(path, session=_session)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        _clear_tool_result_cache(_session)
        return f"Wrote {len(content)} bytes to {path}"
    except Exception as e:
        return _format_tool_error(e)
    
def run_edit(path: str, old_text: str, new_text: str, *, _session=None) -> str:
    try:
        fp = safe_path(path, session=_session)
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        _clear_tool_result_cache(_session)
        return f"Edited {path}"
    except Exception as e:
        return _format_tool_error(e)


def _format_line_window(
    lines: list[str],
    *,
    path_label: str,
    tool_name: str,
    offset: int = 0,
    limit: int | None = None,
    max_chars: int = MAX_WORKSPACE_READ_CHARS,
) -> str:
    total = len(lines)
    offset = min(_nonnegative_int(offset, default=0), total)
    requested_limit = _optional_positive_int(limit)
    requested_end = total if requested_limit is None else min(total, offset + requested_limit)
    selected = lines[offset:requested_end]
    displayed, next_offset, char_limited = _fit_lines_to_chars(
        selected,
        start_offset=offset,
        max_chars=max_chars,
    )
    if not char_limited:
        next_offset = requested_end
    output = "\n".join(displayed)
    if next_offset < total:
        remaining = total - next_offset
        suggested_limit = requested_limit or max(1, next_offset - offset)
        notice = (
            f"[{tool_name}] showed lines {offset}-{next_offset} of {total}. "
            f"To continue: {tool_name}(path=\"{path_label}\", "
            f"offset={next_offset}, limit={suggested_limit}). "
            f"{remaining} lines remain."
        )
        output = f"{output}\n{notice}" if output else notice
    return output


def _normalize_read_file_batch(files) -> list[dict]:
    if not isinstance(files, list):
        raise ValueError("read_files requires files=[{path, offset?, limit?}, ...].")
    if not files:
        raise ValueError("read_files requires at least one file.")
    if len(files) > MAX_BATCH_READ_FILES:
        raise ValueError(f"read_files supports at most {MAX_BATCH_READ_FILES} files per call.")

    normalized: list[dict] = []
    for index, item in enumerate(files):
        if isinstance(item, str):
            item = {"path": item}
        if not isinstance(item, dict):
            raise ValueError(f"read_files item {index} must be an object with a path.")
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValueError(f"read_files item {index} is missing path.")
        normalized.append({
            "path": path,
            "offset": _nonnegative_int(item.get("offset"), default=0),
            "limit": _optional_positive_int(item.get("limit")),
        })
    return normalized


def _should_guard_subagent_large_read(
    session,
    lines: list[str],
    *,
    offset: int,
    limit: int | None,
) -> bool:
    metadata = getattr(session, "metadata", {}) or {}
    return (
        metadata.get("kind") == "subagent"
        and limit is None
        and offset == 0
        and len(lines) > CODE_OUTLINE_LARGE_FILE_LINES
    )


def _format_large_file_outline_notice(lines: list[str], *, path_label: str) -> str:
    preview_limit = min(80, len(lines))
    preview = _format_line_window(
        lines,
        path_label=path_label,
        tool_name="read_file",
        offset=0,
        limit=preview_limit,
        max_chars=MAX_WORKSPACE_READ_CHARS,
    )
    notice = (
        f"[read_file] large file guard: {path_label} has {len(lines)} lines, "
        f"above the subagent threshold {CODE_OUTLINE_LARGE_FILE_LINES}. "
        f"Call code_outline(path=\"{path_label}\") first, then use targeted "
        "read_file(path=..., offset=..., limit=...) windows for the needed symbols."
    )
    return f"{preview}\n{notice}" if preview else notice


def _fit_lines_to_chars(
    lines: list[str],
    *,
    start_offset: int,
    max_chars: int,
) -> tuple[list[str], int, bool]:
    max_chars = max(200, int(max_chars or MAX_WORKSPACE_READ_CHARS))
    reserve = 500
    budget = max(80, max_chars - reserve)
    rendered: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        separator = 1 if rendered else 0
        addition = separator + len(line)
        if used + addition > budget:
            if not rendered:
                keep = max(20, budget - len("...[line truncated]"))
                rendered.append(line[:keep].rstrip() + "...[line truncated]")
                return rendered, start_offset + 1, True
            return rendered, start_offset + index, True
        rendered.append(line)
        used += addition
    return rendered, start_offset + len(lines), False


def _format_repo_map_window(
    lines: list[str],
    *,
    path_label: str,
    max_depth: int,
    include_lines: bool,
    max_symbols: int,
    offset: int = 0,
) -> str:
    total = len(lines)
    offset = min(_nonnegative_int(offset, default=0), total)
    displayed, next_offset, char_limited = _fit_lines_to_chars(
        lines[offset:],
        start_offset=offset,
        max_chars=REPO_MAP_MAX_CHARS,
    )
    output = "\n".join(displayed)
    if char_limited and next_offset < total:
        remaining = total - next_offset
        notice = (
            f"[repo_map] showed lines {offset}-{next_offset} of {total}. "
            "To continue: "
            f"repo_map(path=\"{path_label}\", max_depth={max_depth}, "
            f"include_lines={str(bool(include_lines)).lower()}, "
            f"max_symbols={max_symbols}, offset={next_offset}). "
            f"{remaining} lines remain."
        )
        output = f"{output}\n{notice}" if output else notice
    return output


def _format_code_outline_window(
    lines: list[str],
    *,
    path_label: str,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    output = _format_line_window(
        lines,
        path_label=path_label,
        tool_name="code_outline",
        offset=offset,
        limit=limit,
        max_chars=CODE_OUTLINE_MAX_CHARS,
    )
    return output


def _extract_code_symbols(lines: list[str], *, suffix: str) -> list[dict]:
    if suffix == ".py":
        return _extract_python_symbols(lines)
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        return _extract_js_ts_symbols(lines)
    return []


def _extract_python_symbols(lines: list[str]) -> list[dict]:
    text = "\n".join(lines)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_python_symbols_with_regex(lines)

    symbols: list[dict] = []
    class_stack: list[str] = []

    class Visitor(ast.NodeVisitor):
        def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
            symbols.append(_python_ast_symbol(node, "class", lines, class_stack))
            class_stack.append(node.name)
            self.generic_visit(node)
            class_stack.pop()

        def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
            kind = "method" if class_stack else "function"
            symbols.append(_python_ast_symbol(node, kind, lines, class_stack))
            self.generic_visit(node)

        def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
            kind = "method" if class_stack else "function"
            symbols.append(_python_ast_symbol(node, kind, lines, class_stack))
            self.generic_visit(node)

    Visitor().visit(tree)
    return sorted(symbols, key=lambda item: (int(item["line"]), item["name"]))


def _python_ast_symbol(
    node: ast.AST,
    kind: str,
    lines: list[str],
    class_stack: list[str],
) -> dict:
    line_no = int(getattr(node, "lineno", 1) or 1)
    name = str(getattr(node, "name", ""))
    item = {
        "name": name,
        "kind": kind,
        "line": line_no,
        "signature": _definition_signature(lines, line_no),
    }
    if class_stack:
        item["container"] = ".".join(class_stack)
    return item


def _extract_python_symbols_with_regex(lines: list[str]) -> list[dict]:
    symbols: list[dict] = []
    for index, line in enumerate(lines, 1):
        symbol = _python_symbol(line, index, lines=lines)
        if symbol is not None:
            symbols.append(symbol)
    return symbols


def _python_symbol(line: str, line_no: int, *, lines: list[str]) -> dict | None:
    match = re.match(r"^(?P<indent>\s*)(?P<kind>class|def)\s+(?P<name>[A-Za-z_]\w*)", line)
    if not match:
        match = re.match(r"^(?P<indent>\s*)async\s+def\s+(?P<name>[A-Za-z_]\w*)", line)
        if not match:
            return None
        kind = "method" if match.group("indent") else "function"
        return {
            "name": match.group("name"),
            "kind": kind,
            "line": line_no,
            "signature": _definition_signature(lines, line_no),
        }
    indent = match.group("indent")
    raw_kind = match.group("kind")
    if raw_kind == "class":
        kind = "class"
    else:
        kind = "method" if indent else "function"
    return {
        "name": match.group("name"),
        "kind": kind,
        "line": line_no,
        "signature": _definition_signature(lines, line_no),
    }


def _extract_js_ts_symbols(lines: list[str]) -> list[dict]:
    symbols: list[dict] = []
    for index, line in enumerate(lines, 1):
        symbol = _js_ts_symbol(line, index, lines=lines)
        if symbol is not None:
            symbols.append(symbol)
    return symbols


def _js_ts_symbol(line: str, line_no: int, *, lines: list[str]) -> dict | None:
    stripped = line.strip()
    patterns = [
        (r"^(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_$][\w$]*)", "function"),
        (r"^(?:export\s+)?class\s+([A-Za-z_$][\w$]*)", "class"),
        (r"^(?:export\s+)?const\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?\(", "function"),
        (r"^([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{", "method"),
    ]
    for pattern, kind in patterns:
        match = re.match(pattern, stripped)
        if match:
            return {
                "name": match.group(1),
                "kind": kind,
                "line": line_no,
                "signature": _definition_signature(lines, line_no),
            }
    return None


def _definition_signature(lines: list[str], line_no: int) -> str:
    if line_no <= 0 or line_no > len(lines):
        return ""
    parts = [lines[line_no - 1].strip()]
    balance = _delimiter_balance(parts[0])
    next_line = line_no
    while next_line < len(lines) and len(parts) < 5 and balance > 0:
        candidate = lines[next_line].strip()
        if not candidate:
            break
        parts.append(candidate)
        balance += _delimiter_balance(candidate)
        if candidate.endswith(("{", ":", "=>")) and balance <= 0:
            break
        next_line += 1
    signature = " ".join(part for part in parts if part)
    return signature[:240]


def _delimiter_balance(text: str) -> int:
    return text.count("(") + text.count("[") + text.count("{") - text.count(")") - text.count("]") - text.count("}")


def _extract_code_references(text: str, *, suffix: str) -> Counter:
    if suffix == ".py":
        return _extract_python_references(text)
    return _extract_token_references(text)


def _extract_python_references(text: str) -> Counter:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return _extract_token_references(text)

    refs: Counter = Counter()

    class RefVisitor(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:  # noqa: N802
            if isinstance(node.ctx, ast.Load) and _repo_identifier_ok(node.id):
                refs[node.id] += 1

        def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
            if _repo_identifier_ok(node.attr):
                refs[node.attr] += 1
            self.generic_visit(node)

    RefVisitor().visit(tree)
    return refs


def _extract_token_references(text: str) -> Counter:
    refs: Counter = Counter()
    for name in REPO_MAP_IDENTIFIER_RE.findall(text):
        if _repo_identifier_ok(name):
            refs[name] += 1
    return refs


def _repo_identifier_ok(name: str) -> bool:
    if not name or len(name) < 3:
        return False
    lowered = name.lower()
    if lowered in REPO_MAP_IDENTIFIER_STOPWORDS or keyword.iskeyword(name):
        return False
    if name.startswith("__") and name.endswith("__"):
        return False
    return any(char.isalpha() for char in name)


def _build_ranked_symbol_map_payload(
    *,
    root: Path,
    file_paths: list[Path],
    max_symbols: int,
    focus: dict[str, list[str]],
) -> dict:
    source_files: list[str] = []
    symbols_by_file: dict[str, list[dict]] = defaultdict(list)
    references_by_ident: dict[str, Counter] = defaultdict(Counter)
    definitions_by_ident: dict[str, list[dict]] = defaultdict(list)
    skipped_symbol_files = 0

    for file_path in file_paths:
        suffix = file_path.suffix.lower()
        if suffix not in REPO_MAP_SOURCE_SUFFIXES:
            continue
        rel = file_path.relative_to(root).as_posix()
        text = _repo_file_text(file_path)
        if text is None:
            skipped_symbol_files += 1
            continue
        lines = text.splitlines()
        source_files.append(rel)
        for symbol in _extract_code_symbols(lines, suffix=suffix):
            name = str(symbol.get("name") or "")
            if not _repo_identifier_ok(name):
                continue
            enriched = {
                **symbol,
                "path": rel,
                "signature": str(symbol.get("signature") or "")[:240],
            }
            symbols_by_file[rel].append(enriched)
            definitions_by_ident[name].append(enriched)
        for ident, count in _extract_code_references(text, suffix=suffix).items():
            if count > 0:
                references_by_ident[ident][rel] += count

    source_files = sorted(set(source_files))
    all_symbols = [symbol for path in source_files for symbol in symbols_by_file.get(path, [])]
    ranked_symbols, ranked_files = _rank_repo_symbols(
        source_files=source_files,
        all_symbols=all_symbols,
        symbols_by_file=symbols_by_file,
        references_by_ident=references_by_ident,
        definitions_by_ident=definitions_by_ident,
        focus=focus,
        max_symbols=max_symbols,
    )
    return {
        "symbol_rank_algorithm": "reference_pagerank",
        "symbol_source_files": len(source_files),
        "symbol_files_skipped": skipped_symbol_files,
        "symbol_count": len(all_symbols),
        "focus": focus,
        "ranked_files": ranked_files,
        "ranked_symbols": ranked_symbols,
        "symbol_map": _render_symbol_map_lines(ranked_symbols),
    }


def _repo_file_text(path: Path) -> str | None:
    try:
        if path.stat().st_size > REPO_MAP_MAX_FILE_BYTES:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _rank_repo_symbols(
    *,
    source_files: list[str],
    all_symbols: list[dict],
    symbols_by_file: dict[str, list[dict]],
    references_by_ident: dict[str, Counter],
    definitions_by_ident: dict[str, list[dict]],
    focus: dict[str, list[str]],
    max_symbols: int,
) -> tuple[list[dict], list[dict]]:
    if not source_files or not all_symbols:
        return [], []

    edges = _repo_map_symbol_edges(
        references_by_ident=references_by_ident,
        definitions_by_ident=definitions_by_ident,
        focus=focus,
    )
    file_scores = _weighted_pagerank(source_files, edges, focus=focus) if edges else {}
    if not file_scores:
        file_scores = _fallback_file_scores(source_files, symbols_by_file, focus=focus)

    outgoing: Counter = Counter()
    for src, _dst, _ident, weight in edges:
        outgoing[src] += weight

    definition_scores: Counter = Counter()
    for src, dst, ident, weight in edges:
        total = outgoing[src]
        if total <= 0:
            continue
        definition_scores[(dst, ident)] += file_scores.get(src, 0.0) * weight / total

    focus_files = set(focus.get("files", []))
    focus_identifiers = set(focus.get("identifiers", []))
    ranked: list[dict] = []
    for symbol in all_symbols:
        path = str(symbol["path"])
        name = str(symbol["name"])
        score = float(definition_scores[(path, name)])
        score += float(file_scores.get(path, 0.0)) * 0.03
        score += _symbol_kind_bonus(str(symbol.get("kind") or "")) * 0.0001
        if name in focus_identifiers:
            score *= 4.0
            score += 0.2
        if _repo_path_is_focused(path, focus_files):
            score *= 2.0
            score += 0.05
        ranked.append({
            "path": path,
            "name": name,
            "kind": symbol.get("kind", ""),
            "line": int(symbol.get("line") or 0),
            "rank": round(score, 8),
            "signature": symbol.get("signature", ""),
            **({"container": symbol["container"]} if symbol.get("container") else {}),
        })

    ranked.sort(key=lambda item: (-float(item["rank"]), item["path"], int(item["line"]), item["name"]))
    ranked = ranked[:max_symbols]

    ranked_file_items = _ranked_file_items(
        source_files,
        file_scores=file_scores,
        symbols_by_file=symbols_by_file,
        ranked_symbols=ranked,
    )
    return ranked, ranked_file_items


def _repo_map_symbol_edges(
    *,
    references_by_ident: dict[str, Counter],
    definitions_by_ident: dict[str, list[dict]],
    focus: dict[str, list[str]],
) -> list[tuple[str, str, str, float]]:
    edges: list[tuple[str, str, str, float]] = []
    focus_identifiers = set(focus.get("identifiers", []))
    for ident in sorted(set(references_by_ident).intersection(definitions_by_ident)):
        definers = sorted({str(symbol["path"]) for symbol in definitions_by_ident[ident]})
        if not definers:
            continue
        multiplier = _identifier_rank_multiplier(
            ident,
            definer_count=len(definers),
            focus_identifiers=focus_identifiers,
        )
        for src, count in sorted(references_by_ident[ident].items()):
            if count <= 0:
                continue
            for dst in definers:
                weight = multiplier * (float(count) ** 0.5)
                if src == dst:
                    weight *= 0.25
                if weight > 0:
                    edges.append((src, dst, ident, weight))
    return edges


def _identifier_rank_multiplier(
    ident: str,
    *,
    definer_count: int,
    focus_identifiers: set[str],
) -> float:
    multiplier = 1.0
    if ident in focus_identifiers:
        multiplier *= 10.0
    is_snake = "_" in ident and any(char.isalpha() for char in ident)
    is_kebab = "-" in ident and any(char.isalpha() for char in ident)
    is_camel = any(char.isupper() for char in ident) and any(char.islower() for char in ident)
    if (is_snake or is_kebab or is_camel) and len(ident) >= 8:
        multiplier *= 4.0
    if ident.lower() in REPO_MAP_GENERIC_IDENTIFIERS:
        multiplier *= 0.05
    if len(ident) <= 4 and definer_count > 1:
        multiplier *= 0.05
    if ident.startswith("_"):
        multiplier *= 0.2
    if definer_count > 5:
        multiplier *= 0.2
    return multiplier


def _weighted_pagerank(
    source_files: list[str],
    edges: list[tuple[str, str, str, float]],
    *,
    focus: dict[str, list[str]],
    damping: float = 0.85,
    iterations: int = 28,
) -> dict[str, float]:
    nodes = sorted(set(source_files))
    if not nodes:
        return {}
    node_set = set(nodes)
    personalization = _repo_map_personalization(nodes, focus=focus)
    ranks = dict(personalization)
    incoming: dict[str, list[tuple[str, float]]] = defaultdict(list)
    outgoing: Counter = Counter()
    for src, dst, _ident, weight in edges:
        if src not in node_set or dst not in node_set or weight <= 0:
            continue
        incoming[dst].append((src, weight))
        outgoing[src] += weight

    if not incoming:
        return personalization

    for _ in range(iterations):
        dangling = sum(ranks[node] for node in nodes if outgoing[node] <= 0)
        next_ranks: dict[str, float] = {}
        for node in nodes:
            rank = (1.0 - damping) * personalization[node]
            rank += damping * dangling * personalization[node]
            for src, weight in incoming.get(node, []):
                rank += damping * ranks[src] * weight / outgoing[src]
            next_ranks[node] = rank
        total = sum(next_ranks.values()) or 1.0
        ranks = {node: value / total for node, value in next_ranks.items()}
    return ranks


def _repo_map_personalization(source_files: list[str], *, focus: dict[str, list[str]]) -> dict[str, float]:
    focus_files = set(focus.get("files", []))
    focus_identifiers = set(focus.get("identifiers", []))
    weights: dict[str, float] = {}
    for path in source_files:
        weight = 1.0
        if _repo_path_is_focused(path, focus_files):
            weight += 20.0
        components = set(Path(path).parts)
        components.add(Path(path).stem)
        if components.intersection(focus_identifiers):
            weight += 6.0
        weights[path] = weight
    total = sum(weights.values()) or 1.0
    return {path: weight / total for path, weight in weights.items()}


def _fallback_file_scores(
    source_files: list[str],
    symbols_by_file: dict[str, list[dict]],
    *,
    focus: dict[str, list[str]],
) -> dict[str, float]:
    personalization = _repo_map_personalization(source_files, focus=focus)
    weights = {
        path: personalization.get(path, 0.0) + max(1, len(symbols_by_file.get(path, []))) * 0.01
        for path in source_files
    }
    total = sum(weights.values()) or 1.0
    return {path: value / total for path, value in weights.items()}


def _ranked_file_items(
    source_files: list[str],
    *,
    file_scores: dict[str, float],
    symbols_by_file: dict[str, list[dict]],
    ranked_symbols: list[dict],
) -> list[dict]:
    selected_symbol_counts: Counter = Counter(symbol["path"] for symbol in ranked_symbols)
    items = []
    for path in source_files:
        items.append({
            "path": path,
            "rank": round(float(file_scores.get(path, 0.0)), 8),
            "symbol_count": len(symbols_by_file.get(path, [])),
            "selected_symbols": int(selected_symbol_counts[path]),
        })
    items.sort(key=lambda item: (-float(item["rank"]), item["path"]))
    return items[: max(20, min(len(items), REPO_MAP_DEFAULT_MAX_SYMBOLS))]


def _render_symbol_map_lines(ranked_symbols: list[dict]) -> list[str]:
    by_path: dict[str, list[dict]] = defaultdict(list)
    for symbol in ranked_symbols:
        by_path[str(symbol["path"])].append(symbol)

    lines: list[str] = []
    for path in sorted(by_path):
        lines.append(f"{path}:")
        last_line = -1
        for symbol in sorted(by_path[path], key=lambda item: (int(item["line"]), item["name"])):
            line_no = int(symbol["line"])
            if last_line >= 0 and line_no - last_line > 3:
                lines.append("  ...")
            container = f"{symbol['container']}." if symbol.get("container") else ""
            signature = str(symbol.get("signature") or symbol["name"]).strip()
            lines.append(f"  L{line_no}: {container}{signature}")
            last_line = line_no
    return lines


def _symbol_kind_bonus(kind: str) -> float:
    if kind == "class":
        return 3.0
    if kind == "function":
        return 2.0
    if kind == "method":
        return 1.0
    return 0.0


def _repo_map_focus_from_session(session) -> dict[str, list[str]]:
    text = _recent_user_text(session)
    files = sorted(set(REPO_MAP_FILE_RE.findall(text)))[:50]
    identifiers = []
    seen = set()
    for name in REPO_MAP_IDENTIFIER_RE.findall(text):
        if not _repo_identifier_ok(name) or name in seen:
            continue
        seen.add(name)
        identifiers.append(name)
        if len(identifiers) >= 80:
            break
    return {
        "files": files,
        "identifiers": identifiers,
    }


def _recent_user_text(session) -> str:
    if session is None:
        return ""
    messages = list(getattr(session, "messages", []) or [])[-12:]
    chunks: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") != "user":
            continue
        chunks.append(_message_content_text(message.get("content")))
    return "\n".join(chunks)


def _message_content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content or "")


def _repo_path_is_focused(path: str, focus_files: set[str]) -> bool:
    if not focus_files:
        return False
    normalized = path.strip("/")
    return any(normalized == item.strip("/") or normalized.endswith("/" + item.strip("/")) for item in focus_files)


def _repo_file_paths(root: Path, target: Path, normalized_path: str) -> tuple[list[Path], str]:
    if (root / ".git").exists():
        try:
            pathspec = "." if normalized_path == "." else normalized_path
            result = subprocess.run(
                [
                    "git",
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    pathspec,
                ],
                cwd=root,
                capture_output=True,
                timeout=30,
            )
            if result.returncode == 0:
                paths = []
                for raw in result.stdout.split(b"\0"):
                    if not raw:
                        continue
                    rel = raw.decode("utf-8", errors="replace")
                    file_path = (root / rel).resolve()
                    if file_path.is_file() and file_path.is_relative_to(target):
                        paths.append(file_path)
                return sorted(set(paths), key=lambda item: item.relative_to(root).as_posix()), "git"
        except (OSError, subprocess.TimeoutExpired):
            pass
    return _filesystem_repo_paths(root, target), "filesystem"


def _filesystem_repo_paths(root: Path, target: Path) -> list[Path]:
    paths = []
    for item in target.rglob("*"):
        try:
            resolved = item.resolve()
            rel_parts = resolved.relative_to(root).parts
        except ValueError:
            continue
        if not item.is_file():
            continue
        if any(part.startswith(".") for part in rel_parts):
            continue
        if "__pycache__" in rel_parts:
            continue
        paths.append(resolved)
    return sorted(paths, key=lambda item: item.relative_to(root).as_posix())


def _build_repo_map_payload(
    *,
    root: Path,
    target: Path,
    file_paths: list[Path],
    max_depth: int,
    include_lines: bool,
) -> tuple[list[dict], list[dict], dict[str, int]]:
    directories: dict[str, dict[str, int | str]] = {}
    files: list[dict] = []
    total_lines = 0
    skipped = 0

    for file_path in file_paths:
        rel = file_path.relative_to(root).as_posix()
        rel_to_target = file_path.relative_to(target)
        depth = len(rel_to_target.parts)
        line_count = _repo_file_line_count(file_path) if include_lines else None
        if line_count is None and include_lines:
            skipped += 1
        if line_count is not None:
            total_lines += line_count
        _accumulate_repo_directories(
            directories,
            root=root,
            target=target,
            file_path=file_path,
            line_count=line_count,
        )
        if depth <= max_depth:
            item = {
                "path": rel,
                "depth": depth,
                "bytes": file_path.stat().st_size,
            }
            if include_lines:
                item["lines"] = line_count
            files.append(item)

    directory_items = [
        item
        for item in directories.values()
        if int(item["depth"]) <= max_depth
    ]
    directory_items.sort(key=lambda item: str(item["path"]))
    return (
        directory_items,
        files,
        {
            "files": len(file_paths),
            "lines": total_lines,
            "line_count_skipped": skipped,
        },
    )


def _accumulate_repo_directories(
    directories: dict[str, dict[str, int | str]],
    *,
    root: Path,
    target: Path,
    file_path: Path,
    line_count: int | None,
) -> None:
    rel_parent = file_path.parent.relative_to(root).as_posix()
    target_rel_parent = file_path.parent.relative_to(target)
    ancestors = [target]
    current = target
    for part in target_rel_parent.parts:
        current = current / part
        ancestors.append(current)
    for directory in ancestors:
        path = "." if directory == root else directory.relative_to(root).as_posix()
        depth = 0 if directory == target else len(directory.relative_to(target).parts)
        item = directories.setdefault(
            path,
            {
                "path": path,
                "depth": depth,
                "file_count": 0,
                "total_lines": 0,
            },
        )
        item["file_count"] = int(item["file_count"]) + 1
        if line_count is not None:
            item["total_lines"] = int(item["total_lines"]) + line_count
    if rel_parent not in directories and file_path.parent != target:
        directories[rel_parent] = {
            "path": rel_parent,
            "depth": len(file_path.parent.relative_to(target).parts),
            "file_count": 1,
            "total_lines": line_count or 0,
        }


def _repo_file_line_count(path: Path) -> int | None:
    try:
        if path.stat().st_size > REPO_MAP_MAX_FILE_BYTES:
            return None
        with path.open("rb") as handle:
            sample = handle.read(8192)
            if b"\0" in sample:
                return None
            chunks = [sample]
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
        data = b"".join(chunks)
        if not data:
            return 0
        return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
    except OSError:
        return None


def _nonnegative_int(value, *, default: int = 0) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, parsed)


def _optional_nonnegative_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _optional_positive_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _bounded_positive_int(value, *, default: int, maximum: int) -> int:
    parsed = _optional_positive_int(value)
    if parsed is None:
        parsed = default
    return min(max(1, parsed), maximum)


def _as_bool(value, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _rg_globs(value) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, list):
        items = value
    else:
        items = [value]
    globs = []
    for item in items:
        text = str(item or "").strip()
        if text:
            globs.append(text)
    return globs[:20]


def _number_lines(
    lines: list[str],
    *,
    width: int,
    number_blank_lines: bool,
) -> list[str]:
    rendered = []
    for line_no, line in enumerate(lines, 1):
        if line or number_blank_lines:
            rendered.append(f"{line_no:>{width}}\t{line}")
        else:
            rendered.append(f"{'':>{width}}\t{line}")
    return rendered


def _compile_grep_matcher(
    pattern: str,
    *,
    case_sensitive: bool,
    literal: bool,
):
    if literal:
        needle = pattern if case_sensitive else pattern.lower()

        def match_literal(line: str) -> bool:
            haystack = line if case_sensitive else line.lower()
            return needle in haystack

        return match_literal

    flags = 0 if case_sensitive else re.IGNORECASE
    regex = re.compile(pattern, flags)
    return lambda line: regex.search(line) is not None


def _grep_files(root: Path, target: Path, *, recursive: bool) -> list[Path]:
    if target.is_file():
        return [target]
    iterator = target.rglob("*") if recursive else target.iterdir()
    files = []
    for item in sorted(iterator, key=lambda path: path.relative_to(root).as_posix()):
        if not item.is_file():
            continue
        rel_parts = item.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts):
            continue
        if "__pycache__" in rel_parts:
            continue
        files.append(item)
    return files


def _path_matches_globs(path: str, globs: list[str]) -> bool:
    positives = []
    for glob in globs:
        if glob.startswith("!"):
            pattern = glob[1:]
            if pattern and _path_matches_one_glob(path, pattern):
                return False
        else:
            positives.append(glob)
    return not positives or any(_path_matches_one_glob(path, glob) for glob in positives)


def _path_matches_one_glob(path: str, glob: str) -> bool:
    return fnmatch.fnmatch(path, glob) or fnmatch.fnmatch(Path(path).name, glob)


def _tool_cache_key(tool_name: str, arguments: dict) -> str:
    return json.dumps(
        {
            "tool": tool_name,
            "arguments": arguments,
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )


def _tool_cache_get(session, cache_key: str) -> str | None:
    if session is None:
        return None
    metadata = getattr(session, "metadata", {}) or {}
    cache = metadata.get(TOOL_RESULT_CACHE_KEY)
    if not isinstance(cache, dict):
        return None
    cached = cache.get(cache_key)
    if not isinstance(cached, dict):
        return None
    output = str(cached.get("output") or "")
    step = cached.get("step")
    if not output:
        return None
    return f"[tool-cache] already read at step {step}; unchanged.\n{output}"


def _tool_cache_set(session, cache_key: str, output: str) -> None:
    if session is None or not output or output.startswith("Error:"):
        return
    metadata = getattr(session, "metadata", None)
    if metadata is None:
        return
    cache = metadata.get(TOOL_RESULT_CACHE_KEY)
    if not isinstance(cache, dict):
        cache = {}
    step = _nonnegative_int(metadata.get(TOOL_CACHE_STEP_KEY), default=0) + 1
    metadata[TOOL_CACHE_STEP_KEY] = step
    cache[cache_key] = {
        "output": output,
        "step": step,
    }
    metadata[TOOL_RESULT_CACHE_KEY] = cache


def _clear_tool_result_cache(session) -> None:
    if session is None:
        return
    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, dict):
        metadata.pop(TOOL_RESULT_CACHE_KEY, None)


MAX_STORAGE_READ_BYTES = 1_000_000
MAX_STORAGE_WRITE_BYTES = 10 * 1024 * 1024
MAX_STORAGE_LIST_ENTRIES = 500
MAX_STORAGE_READ_CHARS = 50_000
MAX_PUBLISHED_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_GIT_OUTPUT_CHARS = 50_000
DEFAULT_SANDBOX_TTL_HOURS = 168
STORAGE_TEXT_SUFFIXES = {
    ".csv",
    ".html",
    ".json",
    ".log",
    ".md",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}


def _storage_root(session=None) -> Path:
    return storage_root_for_session(WORKDIR, session)


def _generated_storage_root(session=None) -> Path:
    storage_root = _storage_root(session)
    generated = (storage_root / "generated").resolve()
    if generated != storage_root and not generated.is_relative_to(storage_root):
        raise ValueError("Generated storage directory escapes storage.")
    return generated


def _sandbox_root() -> Path:
    workspace = WORKDIR.resolve()
    root = (workspace / ".task_sandbox").resolve()
    if root != workspace and not root.is_relative_to(workspace):
        raise ValueError("Sandbox directory escapes workspace.")
    return root


def _safe_storage_path(root: Path, raw_path: str, *, allow_root: bool = False) -> Path:
    if not isinstance(raw_path, str):
        raise ValueError("Storage path must be a string.")
    cleaned = raw_path.strip()
    relative = Path(cleaned)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Storage path escapes allowed directory: {raw_path}")
    if any(part.startswith(".") for part in relative.parts):
        raise ValueError("Hidden storage paths are not allowed.")

    root = root.resolve()
    target = (root / relative).resolve()
    if target != root and not target.is_relative_to(root):
        raise ValueError(f"Storage path escapes allowed directory: {raw_path}")
    if target == root and not allow_root:
        raise ValueError("Storage file path is required.")
    return target


def _storage_relative(path: Path, *, session=None) -> str:
    return path.relative_to(_storage_root(session)).as_posix()


def _scope_relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _git_workspace(session=None) -> Path:
    root = workspace_root_for_session(session).resolve()
    if not (root / ".git").exists():
        raise ValueError(f"Workspace is not a git repository root: {root}")
    return root


def _run_git(args: list[str], *, _session=None) -> str:
    root = _git_workspace(_session)
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=60,
    )
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        return f"Error: git {' '.join(args)} failed with code {result.returncode}\n{output}"
    return output[:MAX_GIT_OUTPUT_CHARS] if output else "(no output)"


def _git_pathspec(path: str, *, _session=None) -> str:
    raw = str(path or "").strip()
    if raw.startswith(":"):
        raise ValueError(f"Git pathspec magic is not allowed: {path}")
    target = safe_workspace_path(path, session=_session)
    root = workspace_root_for_session(_session).resolve()
    if target == root:
        raise ValueError("Git path must point to a file or subdirectory, not workspace root.")
    return target.relative_to(root).as_posix()


def _git_pathspecs(paths, *, _session=None) -> list[str]:
    if not isinstance(paths, list) or not paths:
        raise ValueError("paths must be a non-empty list.")
    return [_git_pathspec(str(path), _session=_session) for path in paths]


def run_git_status(porcelain: bool = False, *, _session=None) -> str:
    try:
        args = ["status", "--short"] if porcelain else ["status", "--branch", "--short"]
        return _run_git(args, _session=_session)
    except Exception as e:
        return _format_tool_error(e)


def run_git_diff(
    path: str = "",
    staged: bool = False,
    stat: bool = False,
    *,
    _session=None,
) -> str:
    try:
        args = ["diff"]
        if staged:
            args.append("--staged")
        if stat:
            args.append("--stat")
        if path:
            args.extend(["--", _git_pathspec(path, _session=_session)])
        return _run_git(args, _session=_session)
    except Exception as e:
        return _format_tool_error(e)


def run_git_log(max_count: int = 10, *, _session=None) -> str:
    try:
        try:
            count = max(1, min(int(max_count or 10), 50))
        except (TypeError, ValueError):
            count = 10
        return _run_git(
            [
                "log",
                f"--max-count={count}",
                "--date=iso",
                "--pretty=format:%h %ad %an %s",
            ],
            _session=_session,
        )
    except Exception as e:
        return _format_tool_error(e)


def run_git_branch(all: bool = False, *, _session=None) -> str:
    try:
        args = ["branch"]
        if all:
            args.append("--all")
        return _run_git(args, _session=_session)
    except Exception as e:
        return _format_tool_error(e)


def run_git_add(paths, *, _session=None) -> str:
    try:
        pathspecs = _git_pathspecs(paths, _session=_session)
        return _run_git(["add", "--", *pathspecs], _session=_session)
    except Exception as e:
        return _format_tool_error(e)


def run_git_commit(message: str, *, _session=None) -> str:
    try:
        cleaned = str(message or "").strip()
        if not cleaned:
            raise ValueError("Commit message is required.")
        return _run_git(
            [
                "-c",
                "user.name=Agent Runtime",
                "-c",
                "user.email=agent@example.invalid",
                "commit",
                "-m",
                cleaned,
            ],
            _session=_session,
        )
    except Exception as e:
        return _format_tool_error(e)


def _sandbox_scope_root(session, *, create: bool = True) -> Path:
    if session is None or not str(getattr(session, "id", "")).strip():
        raise ValueError("Sandbox tools require an active session.")

    cleanup_expired_sandboxes()
    metadata = getattr(session, "metadata", {}) or {}
    sandbox_root = _sandbox_root()
    if metadata.get("kind") == "coding_application":
        task_id = str(metadata.get("task_id", "")).strip()
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", task_id):
            raise ValueError("Task session has an invalid task_id for sandbox scope.")
        raw_scope = sandbox_root / "tasks" / task_id
    else:
        digest = hashlib.sha256(str(session.id).encode("utf-8")).hexdigest()[:20]
        raw_scope = sandbox_root / "sessions" / digest

    scope = raw_scope.resolve()
    if scope != sandbox_root and not scope.is_relative_to(sandbox_root):
        raise ValueError("Session sandbox escapes .task_sandbox.")
    if create:
        scope.mkdir(parents=True, exist_ok=True)
        _touch_sandbox_scope(scope)
    return scope


def _touch_sandbox_scope(scope: Path) -> None:
    if scope.exists():
        os.utime(scope, None)


def cleanup_expired_sandboxes(
    *,
    max_age_seconds: float | None = None,
    now: float | None = None,
) -> int:
    root = _sandbox_root()
    if not root.exists():
        return 0
    if max_age_seconds is None:
        try:
            ttl_hours = float(os.environ.get("TASK_SANDBOX_TTL_HOURS", DEFAULT_SANDBOX_TTL_HOURS))
        except ValueError:
            ttl_hours = float(DEFAULT_SANDBOX_TTL_HOURS)
        max_age_seconds = max(0.0, ttl_hours * 3600)
    if max_age_seconds <= 0:
        return 0

    current_time = time.time() if now is None else now
    removed = 0
    for category in ("tasks", "sessions"):
        category_root = root / category
        if not category_root.exists() or category_root.is_symlink():
            continue
        for scope in category_root.iterdir():
            if scope.is_symlink() or not scope.is_dir():
                continue
            if current_time - scope.stat().st_mtime <= max_age_seconds:
                continue
            shutil.rmtree(scope)
            removed += 1
    return removed


def _read_text_file(
    target: Path,
    *,
    path_label: str,
    limit: int = None,
    offset: int = 0,
    tool_name: str = "read_file",
) -> str:
    if not target.exists():
        raise FileNotFoundError(f"File not found: {path_label}")
    if not target.is_file():
        raise IsADirectoryError(f"Path is not a file: {path_label}")
    if target.stat().st_size > MAX_STORAGE_READ_BYTES:
        raise ValueError(
            f"File is too large to read: maximum is {MAX_STORAGE_READ_BYTES} bytes."
        )

    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    return _format_line_window(
        lines,
        path_label=path_label,
        tool_name=tool_name,
        offset=_nonnegative_int(offset, default=0),
        limit=_optional_positive_int(limit),
        max_chars=MAX_STORAGE_READ_CHARS,
    )


def run_storage_list(path: str = "", *, _session=None) -> str:
    try:
        root = _storage_root(_session)
        root.mkdir(parents=True, exist_ok=True)
        target = _safe_storage_path(root, path, allow_root=True)
        if not target.exists():
            raise FileNotFoundError(f"Storage path not found: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Storage path is not a directory: {path}")

        entries = []
        truncated = False
        children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        for child in children:
            if child.name.startswith("."):
                continue
            resolved = child.resolve()
            if resolved != root and not resolved.is_relative_to(root):
                continue
            if len(entries) >= MAX_STORAGE_LIST_ENTRIES:
                truncated = True
                break
            stat = child.stat()
            entries.append({
                "name": child.name,
                "path": _storage_relative(resolved, session=_session),
                "is_dir": child.is_dir(),
                "bytes": 0 if child.is_dir() else stat.st_size,
            })

        return json.dumps({
            "path": _storage_relative(target, session=_session) if target != root else "",
            "entries": entries,
            "truncated": truncated,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_tool_error(e)


def run_storage_read(path: str, limit: int = None, offset: int = 0, *, _session=None) -> str:
    try:
        target = _safe_storage_path(_storage_root(_session), path)
        return _read_text_file(
            target,
            path_label=path,
            limit=limit,
            offset=offset,
            tool_name="storage_read_file",
        )
    except Exception as e:
        return _format_tool_error(e)


def run_storage_write(path: str, content: str, *, _session=None) -> str:
    try:
        if not isinstance(content, str):
            raise ValueError("Storage artifact content must be text.")
        target = _safe_storage_path(_generated_storage_root(_session), path)
        if target.suffix.lower() not in STORAGE_TEXT_SUFFIXES:
            allowed = ", ".join(sorted(STORAGE_TEXT_SUFFIXES))
            raise ValueError(f"Unsupported storage artifact type. Allowed suffixes: {allowed}")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_STORAGE_WRITE_BYTES:
            raise ValueError(
                f"Storage artifact is too large: maximum is {MAX_STORAGE_WRITE_BYTES} bytes."
            )
        if target.exists():
            raise FileExistsError(
                f"Storage artifact already exists: {_storage_relative(target, session=_session)}. "
                "Choose a new filename."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()

        _append_storage_write_record(
            target=target,
            encoded=encoded,
            session_id=getattr(_session, "id", ""),
            session=_session,
        )
        return json.dumps({
            "status": "created",
            "path": _storage_relative(target, session=_session),
            "bytes": len(encoded),
        }, ensure_ascii=False)
    except Exception as e:
        return _format_tool_error(e)


def _append_storage_write_record(
    *,
    target: Path,
    encoded: bytes,
    session_id: str,
    session=None,
) -> None:
    _append_artifact_record(
        operation="storage_write",
        target=target,
        session_id=session_id,
        byte_count=len(encoded),
        sha256=hashlib.sha256(encoded).hexdigest(),
        session=session,
    )


def _append_artifact_record(
    *,
    operation: str,
    target: Path,
    session_id: str,
    byte_count: int,
    sha256: str,
    source_path: str = "",
    session=None,
) -> None:
    records_dir = _storage_root(session) / "records"
    records_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "operation": operation,
        "session_id": session_id,
        "path": _storage_relative(target, session=session),
        "bytes": byte_count,
        "sha256": sha256,
    }
    if source_path:
        record["source_path"] = source_path
    with (records_dir / "storage_writes.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def run_sandbox_list(path: str = "", *, _session=None) -> str:
    try:
        scope = _sandbox_scope_root(_session)
        target = _safe_storage_path(scope, path, allow_root=True)
        if not target.exists():
            raise FileNotFoundError(f"Sandbox path not found: {path}")
        if not target.is_dir():
            raise NotADirectoryError(f"Sandbox path is not a directory: {path}")

        entries = []
        truncated = False
        children = sorted(target.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower()))
        for child in children:
            if child.name.startswith("."):
                continue
            resolved = child.resolve()
            if resolved != scope and not resolved.is_relative_to(scope):
                continue
            if len(entries) >= MAX_STORAGE_LIST_ENTRIES:
                truncated = True
                break
            stat = child.stat()
            entries.append({
                "name": child.name,
                "path": _scope_relative(scope, resolved),
                "is_dir": child.is_dir(),
                "bytes": 0 if child.is_dir() else stat.st_size,
            })
        _touch_sandbox_scope(scope)
        return json.dumps({
            "path": _scope_relative(scope, target) if target != scope else "",
            "entries": entries,
            "truncated": truncated,
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        return _format_tool_error(e)


def run_sandbox_read(path: str, limit: int = None, offset: int = 0, *, _session=None) -> str:
    try:
        scope = _sandbox_scope_root(_session)
        target = _safe_storage_path(scope, path)
        output = _read_text_file(
            target,
            path_label=path,
            limit=limit,
            offset=offset,
            tool_name="sandbox_read_file",
        )
        _touch_sandbox_scope(scope)
        return output
    except Exception as e:
        return _format_tool_error(e)


def run_sandbox_write(
    path: str,
    content: str,
    *,
    overwrite: bool = False,
    _session=None,
) -> str:
    try:
        if not isinstance(content, str):
            raise ValueError("Sandbox file content must be text.")
        encoded = content.encode("utf-8")
        if len(encoded) > MAX_STORAGE_WRITE_BYTES:
            raise ValueError(
                f"Sandbox file is too large: maximum is {MAX_STORAGE_WRITE_BYTES} bytes."
            )

        scope = _sandbox_scope_root(_session)
        target = _safe_storage_path(scope, path)
        existed = target.exists()
        if existed and not overwrite:
            raise FileExistsError(
                f"Sandbox file already exists: {path}. Set overwrite=true to revise it."
            )
        if existed and not target.is_file():
            raise IsADirectoryError(f"Sandbox path is not a file: {path}")

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()
        _touch_sandbox_scope(scope)
        return json.dumps({
            "status": "updated" if existed else "created",
            "path": _scope_relative(scope, target),
            "bytes": len(encoded),
        }, ensure_ascii=False)
    except Exception as e:
        return _format_tool_error(e)


def run_publish_artifact(
    source_path: str,
    destination_path: str | None = None,
    *,
    _session=None,
) -> str:
    try:
        scope = _sandbox_scope_root(_session)
        source = _safe_storage_path(scope, source_path)
        if not source.exists():
            raise FileNotFoundError(f"Sandbox file not found: {source_path}")
        if not source.is_file():
            raise IsADirectoryError(f"Sandbox path is not a file: {source_path}")
        byte_count = source.stat().st_size
        if byte_count > MAX_PUBLISHED_ARTIFACT_BYTES:
            raise ValueError(
                f"Artifact is too large to publish: maximum is {MAX_PUBLISHED_ARTIFACT_BYTES} bytes."
            )

        published_path = destination_path if destination_path is not None else source_path
        target = _safe_storage_path(_generated_storage_root(_session), published_path)
        if target.exists():
            raise FileExistsError(
                f"Storage artifact already exists: {_storage_relative(target, session=_session)}. "
                "Choose a new destination filename."
            )

        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        digest = hashlib.sha256()
        try:
            with source.open("rb") as source_handle, temporary.open("wb") as target_handle:
                while chunk := source_handle.read(1024 * 1024):
                    digest.update(chunk)
                    target_handle.write(chunk)
            temporary.replace(target)
        finally:
            if temporary.exists():
                temporary.unlink()

        _append_artifact_record(
            operation="publish_artifact",
            target=target,
            session_id=getattr(_session, "id", ""),
            source_path=source_path,
            byte_count=byte_count,
            sha256=digest.hexdigest(),
            session=_session,
        )
        _touch_sandbox_scope(scope)
        return json.dumps({
            "status": "published",
            "source_path": source_path,
            "path": _storage_relative(target, session=_session),
            "bytes": byte_count,
        }, ensure_ascii=False)
    except Exception as e:
        return _format_tool_error(e)


def run_bash(command: str, *, _session=None) -> str:
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        workspace = workspace_root_for_session(_session)
        r = subprocess.run(command, shell=True, cwd=workspace,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"
    except (FileNotFoundError, OSError) as e:
        return _format_tool_error(e)
    

BASE_HANDLERS = {
    "bash": lambda **kw: run_bash(kw["command"], _session=kw.get("_session")),
    "load_skill": lambda **kw: SKILL_LOADER.get_content(kw["name"]),
    "list_files": lambda **kw: run_list_files(
        kw.get("path", ""),
        kw.get("recursive", False),
        kw.get("offset", 0),
        _session=kw.get("_session"),
    ),
    "rg": lambda **kw: run_rg(
        kw["pattern"],
        kw.get("path", ""),
        kw.get("glob"),
        kw.get("max_matches"),
        kw.get("case_sensitive", True),
        kw.get("literal", False),
        _session=kw.get("_session"),
    ),
    "nl": lambda **kw: run_nl(
        kw["path"],
        kw.get("offset", 0),
        kw.get("limit"),
        kw.get("number_blank_lines", True),
        kw.get("width"),
        _session=kw.get("_session"),
    ),
    "grep": lambda **kw: run_grep(
        kw["pattern"],
        kw.get("path", ""),
        kw.get("glob"),
        kw.get("max_matches"),
        kw.get("case_sensitive", True),
        kw.get("literal", False),
        kw.get("recursive", True),
        _session=kw.get("_session"),
    ),
    "repo_map": lambda **kw: run_repo_map(
        kw.get("path", ""),
        kw.get("max_depth"),
        kw.get("include_lines", True),
        kw.get("offset", 0),
        max_symbols=kw.get("max_symbols"),
        _session=kw.get("_session"),
    ),
    "code_outline": lambda **kw: run_code_outline(
        kw["path"],
        kw.get("offset", 0),
        kw.get("limit"),
        _session=kw.get("_session"),
    ),
    "read_file": lambda **kw: run_read(
        kw["path"],
        kw.get("limit"),
        kw.get("offset", 0),
        _session=kw.get("_session"),
    ),
    "read_files": lambda **kw: run_read_files(
        kw["files"],
        kw.get("format", kw.get("output_format", "text")),
        _session=kw.get("_session"),
    ),
    "retrieve_tool_result": lambda **kw: run_retrieve_tool_result(
        kw["result_id"],
        kw.get("offset", 0),
        kw.get("limit"),
        kw.get("query"),
        _session=kw.get("_session"),
    ),
    "write_file": lambda **kw: run_write(
        kw["path"],
        kw["content"],
        _session=kw.get("_session"),
    ),
    "edit_file": lambda **kw: run_edit(
        kw["path"],
        kw["old_text"],
        kw["new_text"],
        _session=kw.get("_session"),
    ),
    "git_status": lambda **kw: run_git_status(
        kw.get("porcelain", False),
        _session=kw.get("_session"),
    ),
    "git_diff": lambda **kw: run_git_diff(
        kw.get("path", ""),
        kw.get("staged", False),
        kw.get("stat", False),
        _session=kw.get("_session"),
    ),
    "git_log": lambda **kw: run_git_log(
        kw.get("max_count", 10),
        _session=kw.get("_session"),
    ),
    "git_branch": lambda **kw: run_git_branch(
        kw.get("all", False),
        _session=kw.get("_session"),
    ),
    "git_add": lambda **kw: run_git_add(
        kw["paths"],
        _session=kw.get("_session"),
    ),
    "git_commit": lambda **kw: run_git_commit(
        kw["message"],
        _session=kw.get("_session"),
    ),
}

TASK_HANDLERS = {
    "update_task_state": lambda **kw: _update_task_state(**kw),
    "task_create": lambda **kw: TASKS.create(
        kw["subject"],
        kw.get("description", "")
    ),
    "task_update": lambda **kw: TASKS.update(
        kw["task_id"],
        kw.get("status"),
        kw.get("addBlockedBy"),
        kw.get("removeBlockedBy"),
    ),
    "task_list": lambda **kw: TASKS.list_all(),
    "task_get": lambda **kw: TASKS.get(kw["task_id"]),
}


def _update_task_state(**kwargs):
    mode = str(kwargs.pop("_mode", "") or "")
    if not mode:
        mode = str(getattr(kwargs.get("_session"), "active_agent", "") or "")
    if mode not in {"coding", "teammate"}:
        return _update_task_state_core(**kwargs)
    from applications.coding.state_updates import StatePatch, reduce_task_state
    from applications.coding.task_state import (
        ensure_task_state,
        load_task_state,
        save_task_state,
    )

    session = kwargs.pop("_session", None)
    if session is None:
        raise ValueError("update_task_state requires an active session")
    state = load_task_state(session)
    if state is None:
        objective = next(
            (
                str(message.get("content") or "")
                for message in reversed(list(getattr(session, "messages", []) or []))
                if isinstance(message, dict)
                and str(message.get("role") or "") == "user"
            ),
            "Complete the current coding task",
        )
        state = ensure_task_state(session, objective_summary=objective[:300])
    patch = StatePatch.from_payload(kwargs)
    patch.origin = "tool"
    if not _state_patch_has_changes(patch):
        raise ValueError("update_task_state requires at least one state change")
    updated = reduce_task_state(state, patch)
    save_task_state(session, updated)
    return json.dumps(
        {
            "status": "updated",
            "task_state_version": updated.version,
            "patch_id": patch.patch_id,
            "instruction": (
                "TaskState updated. If another state change is needed, call "
                "update_task_state again with only the additional change."
            ),
        },
        ensure_ascii=False,
    )


def _update_task_state_core(**kwargs):
    from runtime.task_state import (
        TaskStateCorePatch,
        apply_task_state_core_patch,
        ensure_task_state_core,
        load_task_state_core,
        save_task_state_core,
    )

    session = kwargs.pop("_session", None)
    if session is None:
        raise ValueError("update_task_state requires an active session")
    state = load_task_state_core(session)
    if state is None:
        objective = next(
            (
                str(message.get("content") or "")
                for message in reversed(list(getattr(session, "messages", []) or []))
                if isinstance(message, dict)
                and str(message.get("role") or "") == "user"
            ),
            "Answer the user's current request",
        )
        state = ensure_task_state_core(session, objective=objective)
    patch = TaskStateCorePatch.from_payload(kwargs)
    updated = apply_task_state_core_patch(state, patch)
    save_task_state_core(session, updated)
    return json.dumps(
        {
            "status": "updated",
            "task_state_version": updated.version,
            "task_status": updated.status,
        },
        ensure_ascii=False,
    )


def _state_patch_has_changes(patch) -> bool:
    return bool(
        patch.current_focus is not None
        or patch.completion_basis_add
        or patch.requested_status is not None
        or patch.stop_reason is not None
        or patch.pending_replace is not None
        or patch.open_questions_replace is not None
        or patch.blockers_replace is not None
        or patch.phase is not None
        or patch.constraints
        or patch.plan_items
        or patch.completed
        or patch.findings
        or patch.hypotheses
        or patch.decisions
        or patch.pending_actions
        or patch.open_questions
        or patch.blockers
        or patch.evidence
        or patch.artifact_refs
        or patch.coverage
        or patch.plan_transitions
        or patch.action_transitions
        or patch.question_transitions
        or patch.hypothesis_transitions
    )

BACKGROUND_HANDLERS = {
    "background_run": lambda **kw: BG.run(kw["command"]),
    "check_background": lambda **kw: BG.check(kw.get("task_id")),
}

def make_protocol_handlers(sender: str):
    return {
        "idle": lambda **kw: "Entering idle phase. Will poll for new tasks.",
        "shutdown_response": lambda **kw: PROTOCOLS.handle_shutdown_response(
            sender,
            kw["request_id"],
            kw["approve"],
            kw.get("details", ""),
        ),
        "plan_approval_request": lambda **kw: PROTOCOLS.handle_plan_request(
            sender,
            kw["plan"],
        ),
    }


def _message_type(value: str | None) -> MessageType:
    try:
        return MessageType(str(value or "message"))
    except ValueError:
        return MessageType.MESSAGE


def _send_structured_message(
    *,
    sender: str,
    to: str,
    content: str,
    msg_type: str = "message",
    payload: dict | None = None,
) -> str:
    message = AgentMessage(
        sender=sender,
        recipient=to,
        type=_message_type(msg_type),
        payload={"content": content, **(payload or {})},
    )
    return BUS.send(
        message.sender,
        message.recipient,
        message.to_json(),
        message.type.value,
        {
            "id": message.id,
            "recipient": message.recipient,
            "correlation_id": message.correlation_id,
            "payload": message.payload,
            "ttl_seconds": message.ttl_seconds,
        },
    )


def _read_structured_inbox(name: str) -> str:
    messages = BUS.read_inbox(name)
    rendered = []
    for message in messages:
        try:
            PROTOCOLS.notify_message(message)
            rendered.append(render_agent_message(message))
        except Exception:
            rendered.append(json.dumps(message, ensure_ascii=False))
    return "\n\n".join(rendered) if rendered else "Inbox is empty."


def _spawn_teammate_with_protocol(team, name: str, role: str, prompt: str) -> str:
    message = AgentMessage(
        sender="lead",
        recipient=name,
        type=MessageType.TASK_ASSIGN,
        payload={
            "description": f"Initial task for teammate {name}",
            "prompt": prompt,
            "role": role,
        },
    )
    return team.spawn(name, role, message.to_json())


def _broadcast_structured(team, content: str) -> str:
    count = 0
    for name in team.member_names():
        message = AgentMessage(
            sender="lead",
            recipient=name,
            type=MessageType.BROADCAST,
            payload={"content": content},
        )
        BUS.send(
            message.sender,
            message.recipient,
            message.to_json(),
            message.type.value,
            {
                "id": message.id,
                "recipient": message.recipient,
                "payload": message.payload,
                "ttl_seconds": message.ttl_seconds,
            },
        )
        count += 1
    return f"Broadcast to {count} teammates"


def _run_subagent_task(
    runner=None,
    /,
    *,
    prompt: str,
    description: str = "",
    agent_type: str = "explore",
    scope=None,
    objective: str = "",
    deliverable: str = "",
    budget=None,
    _session=None,
    _trace_store=None,
    _run_state=None,
    _parent_span_id: str | None = None,
) -> str:
    if runner is None:
        return "Error: The short-lived subagent task runner is not configured."
    task = {
        "prompt": prompt,
        "description": description,
        "agent_type": agent_type,
        "scope": scope,
        "objective": objective,
        "deliverable": deliverable,
        "budget": budget,
    }
    decision = guard_subagent_dispatch(_session, [task], tool_name="task")
    if not decision.allowed:
        _trace_subagent_rejection(
            _trace_store,
            _run_state,
            decision,
            tool_name="task",
            parent_span_id=_parent_span_id,
        )
        return rejection_response(decision)
    record_subagent_dispatch(_session, [task], tool_name="task")
    _checkpoint_subtasks_dispatched(_session, [task])
    result = runner.run(
        prompt=_compose_subagent_prompt(
            prompt,
            scope=scope,
            objective=objective,
            deliverable=deliverable,
            budget=budget,
        ),
        description=description,
        agent_type=agent_type,
        parent_session=_session,
        trace_store=_trace_store,
        parent_run_state=_run_state,
        parent_span_id=_parent_span_id,
    )
    record_subagent_results(_session, [task], [result.to_dict()])
    _checkpoint_subtask_results(_session, [task], [result.to_dict()])
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


def _compose_subagent_prompt(
    prompt: str,
    *,
    scope=None,
    objective: str = "",
    deliverable: str = "",
    budget=None,
) -> str:
    sections = []
    if objective:
        sections.append(f"Objective: {objective}")
    if scope:
        sections.append("Scope:\n" + json.dumps(scope, ensure_ascii=False, indent=2, default=str))
    if budget:
        sections.append("Budget:\n" + json.dumps(budget, ensure_ascii=False, indent=2, default=str))
    if deliverable:
        sections.append(f"Deliverable: {deliverable}")
    sections.append(str(prompt or "").strip())
    return "\n\n".join(section for section in sections if section)


def _run_parallel_subagent_tasks(
    runner=None,
    /,
    *,
    tasks,
    max_workers: int | None = None,
    _session=None,
    _trace_store=None,
    _run_state=None,
    _parent_span_id: str | None = None,
) -> str:
    if runner is None:
        return "Error: The short-lived subagent task runner is not configured."
    from agents.subagent.parallel import run_parallel_tasks

    bounded_tasks = list(tasks or [])
    if not bounded_tasks:
        return json.dumps({"results": []}, ensure_ascii=False, indent=2)
    decision = guard_subagent_dispatch(_session, bounded_tasks, tool_name="parallel_tasks")
    if not decision.allowed:
        _trace_subagent_rejection(
            _trace_store,
            _run_state,
            decision,
            tool_name="parallel_tasks",
            parent_span_id=_parent_span_id,
        )
        return rejected_parallel_response(decision)
    record_subagent_dispatch(_session, bounded_tasks, tool_name="parallel_tasks")
    _checkpoint_subtasks_dispatched(_session, bounded_tasks)
    results = run_parallel_tasks(
        runner=runner,
        tasks=bounded_tasks,
        parent_session=_session,
        max_workers=max_workers,
        trace_store=_trace_store,
        parent_run_state=_run_state,
        parent_span_id=_parent_span_id,
    )
    record_subagent_results(_session, bounded_tasks, results)
    _checkpoint_subtask_results(_session, bounded_tasks, results)
    return json.dumps({"results": results}, ensure_ascii=False, indent=2)


def _checkpoint_subtasks_dispatched(session, tasks: list[dict]) -> None:
    if session is None:
        return
    append_event = getattr(session, "append_event", None)
    if callable(append_event):
        append_event("subagents_dispatched", {"tasks": list(tasks)})


def _checkpoint_subtask_results(
    session,
    tasks: list[dict],
    results: list[dict],
) -> None:
    if session is None:
        return
    append_event = getattr(session, "append_event", None)
    if callable(append_event):
        append_event("subagents_completed", {
            "tasks": list(tasks),
            "results": list(results),
        })


def _trace_subagent_rejection(
    trace_store,
    run_state,
    decision,
    *,
    tool_name: str,
    parent_span_id: str | None = None,
) -> None:
    if trace_store is None or run_state is None:
        return
    trace_store.append_event(
        run_state,
        "subagent.fanout.rejected",
        rejection_trace_payload(decision, tool_name=tool_name),
        parent_span_id=parent_span_id,
    )


def make_subagent_handlers(runner=None) -> dict:
    """Bind task tools to one runtime's subagent runner, if available."""
    return {
        "task": partial(_run_subagent_task, runner),
        "parallel_tasks": partial(_run_parallel_subagent_tasks, runner),
    }


def make_lead_handlers(
    team,
    *,
    artifact_store=None,
    subagent_runner=None,
    memory_handlers=None,
):
    return {
        **BASE_HANDLERS,
        **make_artifact_handlers(artifact_store),
        **TASK_HANDLERS,
        **BACKGROUND_HANDLERS,
        **(
            memory_handlers
            if memory_handlers is not None
            else make_memory_handlers()
        ),
        **STORAGE_HANDLERS,
        **SANDBOX_HANDLERS,
        **make_protocol_handlers("lead"),
        **make_subagent_handlers(subagent_runner),

        "compact": lambda **kw: "Manual compression requested.",
        "claim_task": lambda **kw: TASKS.claim_task(
            kw["task_id"],
            "lead",
        ),

        "spawn_teammate": lambda **kw: _spawn_teammate_with_protocol(
            team,
            kw["name"],
            kw["role"],
            kw["prompt"],
        ),
        "list_teammates": lambda **kw: team.list_all(),
        "broadcast": lambda **kw: _broadcast_structured(team, kw["content"]),
        "send_message": lambda **kw: _send_structured_message(
            sender="lead",
            to=kw["to"],
            content=kw["content"],
            msg_type=kw.get("msg_type", "message"),
        ),
        "read_inbox": lambda **kw: _read_structured_inbox("lead"),
        "shutdown_request": lambda **kw: PROTOCOLS.handle_shutdown_request(
            kw["teammate"],
        ),
        "shutdown_status": lambda **kw: PROTOCOLS._check_shutdown_status(
            kw["request_id"],
        ),
        "plan_approval": lambda **kw: PROTOCOLS.handle_plan_review(
            kw["request_id"],
            kw["approve"],
            kw.get("feedback", ""),
        ),
    }



def make_teammate_handlers(name: str, *, artifact_store=None):
    return {
        **BASE_HANDLERS,
        **make_artifact_handlers(artifact_store),
        **TASK_HANDLERS,
        **BACKGROUND_HANDLERS,
        **make_protocol_handlers(name),
        "claim_task": lambda **kw: TASKS.claim_task(
            kw["task_id"],
            name,
        ),

        "send_message": lambda **kw: _send_structured_message(
            sender=name,
            to=kw["to"],
            content=kw["content"],
            msg_type=kw.get("msg_type", "message"),
        ),
        "read_inbox": lambda **kw: _read_structured_inbox(name),
    }

def make_memory_handlers(
    *,
    command_service=None,
    retrieval_service=None,
    index_synchronizer=None,
) -> dict:
    return {
        "memorize": partial(
            run_memorize,
            command_service,
            index_synchronizer,
        ),
        "recall_memory": partial(run_recall_memory, retrieval_service),
    }


def run_memorize(
    command_service=None,
    index_synchronizer=None,
    /,
    *,
    content: str,
    _session=None,
) -> str:
    if command_service is None:
        return "Durable memory is not enabled."
    if command_service is not None:
        from memory.commands import MemoryContext, MemoryWriteProposal
        from memory.domain import (
            MemoryEvidence,
            MemoryKind,
            MemoryOwnerScope,
            MemorySourceType,
        )

        context = MemoryContext.from_session(_session)
        evidence = MemoryEvidence(
            id=str(uuid4()),
            memory_id="pending",
            source_type=MemorySourceType.EXPLICIT_USER,
            source_ref=f"{context.session_id}:tool:memorize",
            session_id=context.session_id,
            excerpt=str(content or "")[:1000],
        )
        item = command_service.remember(
            MemoryWriteProposal(
                content=content,
                kind=MemoryKind.PREFERENCE,
                owner_scope=MemoryOwnerScope.USER,
                owner_id=context.user_id,
                source_type=MemorySourceType.EXPLICIT_USER,
                evidence=(evidence,),
                confidence=1.0,
                salience=0.8,
                explicit_user_request=True,
                metadata={"entrypoint": "memorize_tool"},
            ),
            context,
        )
        if index_synchronizer is not None:
            try:
                index_synchronizer.drain(limit=10)
            except Exception:
                pass
        _queue_memory_trace_events(_session, command_service)
        _queue_memory_trace_events(_session, index_synchronizer)
        return f"Saved semantic memory {item.id} ({item.status.value})."
    return "Durable memory is not enabled."


def run_recall_memory(
    retrieval_service=None,
    /,
    *,
    query: str | None = None,
    _session=None,
) -> str:
    if retrieval_service is not None:
        from memory.commands import MemoryContext

        result = retrieval_service.retrieve(
            str(query or ""),
            MemoryContext.from_session(_session),
        )
        _queue_memory_trace_events(_session, retrieval_service)
        rendered = retrieval_service.render(result)
        return rendered or "No relevant memory found."
    return "Durable memory retrieval is not enabled."


def _queue_memory_trace_events(session, service) -> None:
    if session is None or service is None or not hasattr(service, "drain_trace_events"):
        return
    events = service.drain_trace_events()
    if not events:
        return
    metadata = getattr(session, "metadata", None)
    if isinstance(metadata, dict):
        metadata.setdefault("memory_trace_events", []).extend(events)

STORAGE_HANDLERS = {
    "storage_list_files": lambda **kw: run_storage_list(
        kw.get("path", ""),
        _session=kw.get("_session"),
    ),
    "storage_read_file": lambda **kw: run_storage_read(
        kw["path"],
        kw.get("limit"),
        kw.get("offset", 0),
        _session=kw.get("_session"),
    ),
    "storage_write_file": lambda **kw: run_storage_write(
        kw["path"],
        kw["content"],
        _session=kw.get("_session"),
    ),
}


DEFAULT_ARTIFACT_READ_CHARS = 12_000
MAX_ARTIFACT_READ_CHARS = 50_000


def run_read_artifact(
    artifact_ref: str,
    offset: int = 0,
    limit: int | None = None,
    query: str | None = None,
    max_results: int = 20,
    *,
    artifact_store: ArtifactStore | None = None,
) -> str:
    """Read a bounded range from an immutable context artifact, or search it."""
    store = artifact_store or ArtifactStore(CONTEXT_ARTIFACT_ROOT)
    reference = str(artifact_ref or "").strip()
    if not reference:
        raise ValueError("read_artifact requires a non-empty artifact_ref.")
    try:
        metadata = store.get_artifact_metadata(reference)
    except ArtifactNotFoundError as exc:
        raise ValueError(f"Artifact not found: {reference}") from exc
    text_artifact_types = {"text", "user_input", "tool_result", "json", "log"}
    if (
        metadata.artifact_type not in text_artifact_types
        and not metadata.mime_type.startswith("text/")
        and metadata.mime_type not in {"application/json", "application/xml"}
    ):
        raise ValueError(
            f"Artifact is not readable as text: {metadata.mime_type or 'unknown mime type'}"
        )

    if query is not None:
        needle = str(query)
        if not needle:
            raise ValueError("read_artifact query must not be empty.")
        bounded_results = max(1, min(int(max_results or 20), 50))
        matches = store.search_artifact(
            reference,
            needle,
            max_results=bounded_results,
        )
        bounded_matches = [
            {
                "start": match["start"],
                "end": match["end"],
                "line": match["line"],
                "snippet": match["snippet"],
            }
            for match in matches
        ]
        return json.dumps(
            {
                "artifact_ref": metadata.storage_uri,
                "name": metadata.name,
                "query": needle,
                "matches": bounded_matches,
                "match_count": len(bounded_matches),
                "results_limited_to": bounded_results,
            },
            ensure_ascii=False,
        )

    start = max(0, int(offset or 0))
    if start > metadata.size_chars:
        raise ValueError(
            f"read_artifact offset {start} exceeds artifact size {metadata.size_chars}."
        )
    requested_limit = DEFAULT_ARTIFACT_READ_CHARS if limit is None else int(limit)
    bounded_limit = max(200, min(requested_limit, MAX_ARTIFACT_READ_CHARS))
    end = min(metadata.size_chars, start + bounded_limit)
    content = store.read_artifact_range(reference, start, end)
    truncated = end < metadata.size_chars
    header = json.dumps(
        {
            "artifact_ref": metadata.storage_uri,
            "name": metadata.name,
            "mime_type": metadata.mime_type,
            "size_chars": metadata.size_chars,
            "offset": start,
            "returned_chars": len(content),
            "truncated": truncated,
            "next_offset": end if truncated else None,
        },
        ensure_ascii=False,
    )
    return f"{header}\n\n{content}"


def run_read_artifact_with_metadata(
    artifact_ref: str,
    offset: int = 0,
    limit: int | None = None,
    query: str | None = None,
    max_results: int = 20,
    *,
    artifact_store: ArtifactStore | None = None,
):
    """Return display text plus structured access metadata."""
    from runtime.context.artifact_access import normalize_query
    from tools.executor import ToolHandlerOutput

    store = artifact_store or ArtifactStore(CONTEXT_ARTIFACT_ROOT)
    reference = str(artifact_ref or "").strip()
    try:
        record = store.get_artifact_metadata(reference)
    except ArtifactNotFoundError as exc:
        raise ValueError(f"Artifact not found: {reference}") from exc
    output = run_read_artifact(
        reference,
        offset=offset,
        limit=limit,
        query=query,
        max_results=max_results,
        artifact_store=store,
    )
    if query is not None:
        parsed = json.loads(output)
        access = {
            "artifact_ref": record.storage_uri,
            "mode": "search",
            "normalized_query": normalize_query(query),
            "match_count": int(parsed.get("match_count") or 0),
            "size_chars": record.size_chars,
        }
    else:
        header = json.loads(output.split("\n\n", 1)[0])
        start = int(header.get("offset") or 0)
        end = start + int(header.get("returned_chars") or 0)
        access = {
            "artifact_ref": record.storage_uri,
            "mode": "range",
            "requested_range": [
                max(0, int(offset or 0)),
                max(0, int(offset or 0))
                + max(200, min(int(limit or DEFAULT_ARTIFACT_READ_CHARS), MAX_ARTIFACT_READ_CHARS)),
            ],
            "returned_range": [start, end],
            "size_chars": record.size_chars,
            "truncated": bool(header.get("truncated")),
            "next_offset": header.get("next_offset"),
            "eof": not bool(header.get("truncated")),
        }
    return ToolHandlerOutput(output, metadata={"artifact_access": access})


def make_artifact_handlers(artifact_store=None):
    return {
        "read_artifact": lambda **kw: run_read_artifact_with_metadata(
            kw["artifact_ref"],
            kw.get("offset", 0),
            kw.get("limit"),
            kw.get("query"),
            kw.get("max_results", 20),
            artifact_store=artifact_store,
        ),
    }


# Compatibility alias retained for callers importing the historical handler map.
TEAMMATE_HANDLER = make_teammate_handlers("")


SANDBOX_HANDLERS = {
    "sandbox_list_files": lambda **kw: run_sandbox_list(
        kw.get("path", ""),
        _session=kw.get("_session"),
    ),
    "sandbox_read_file": lambda **kw: run_sandbox_read(
        kw["path"],
        kw.get("limit"),
        kw.get("offset", 0),
        _session=kw.get("_session"),
    ),
    "sandbox_write_file": lambda **kw: run_sandbox_write(
        kw["path"],
        kw["content"],
        overwrite=kw.get("overwrite", False),
        _session=kw.get("_session"),
    ),
    "publish_artifact": lambda **kw: run_publish_artifact(
        kw["source_path"],
        kw.get("destination_path"),
        _session=kw.get("_session"),
    ),
}
