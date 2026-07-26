"""Tool result compression for context-safe reuse."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from runtime.tooling.result_store import tool_result_ref_from_metadata


@dataclass(frozen=True)
class CompressionResult:
    content: str
    strategy: str
    original_chars: int
    compressed_chars: int
    metadata: dict[str, Any] = field(default_factory=dict)


READ_TOOLS = {
    "read_file",
    "read_files",
    "nl",
    "storage_read_file",
    "sandbox_read_file",
    "cat",
}
SEARCH_TOOLS = {"rg", "grep"}
DIFF_TOOLS = {"git_diff"}
GLOBAL_CONTEXT_TOOLS = {"rg", "grep", "repo_map", "code_outline", "list_files"}


def compress_tool_result(
    *,
    tool_name: str,
    message: dict[str, Any],
    content: str,
    max_chars: int = 1800,
) -> CompressionResult:
    tool_name = str(tool_name or "unknown_tool")
    content = str(content or "")
    max_chars = max(300, int(max_chars or 1800))
    args = _message_arguments(message)
    ref = tool_result_ref_from_metadata(message.get("metadata"))

    if tool_name in READ_TOOLS:
        rendered, strategy = _compress_read_result(tool_name, args, content, max_chars)
    elif tool_name in SEARCH_TOOLS:
        rendered, strategy = _compress_search_result(tool_name, args, content, max_chars)
    elif tool_name in DIFF_TOOLS or _looks_like_diff(content):
        rendered, strategy = _compress_diff_result(tool_name, args, content, max_chars)
    elif tool_name == "bash":
        rendered, strategy = _compress_bash_result(args, content, max_chars)
    elif tool_name in {"repo_map", "code_outline", "list_files"}:
        rendered, strategy = _compress_global_context_result(tool_name, args, content, max_chars)
    else:
        rendered, strategy = _compress_unknown_result(tool_name, content, max_chars, ref=ref)

    if ref:
        rendered = _with_retrieval_hint(rendered, ref)

    rendered = _limit_text_middle(rendered, max_chars, marker="\n...[compressed tool result middle trimmed]...\n")
    return CompressionResult(
        content=rendered,
        strategy=strategy,
        original_chars=len(content),
        compressed_chars=len(rendered),
        metadata={
            "compression_strategy": strategy,
            "original_chars": len(content),
            "compressed_chars": len(rendered),
            "recoverable": bool(ref),
            "tool_result_ref": ref,
        },
    )


def compact_tool_result_for_tail(
    *,
    tool_name: str,
    message: dict[str, Any],
    content: str,
    max_chars: int,
) -> str:
    result = compress_tool_result(
        tool_name=tool_name,
        message=message,
        content=content,
        max_chars=max_chars,
    )
    return result.content


def _compress_read_result(
    tool_name: str,
    args: dict[str, Any],
    content: str,
    max_chars: int,
) -> tuple[str, str]:
    path = str(args.get("path") or "")
    offset = args.get("offset", 0)
    limit = args.get("limit")
    limit_part = f", limit={limit}" if limit not in (None, "") else ""
    descriptor = (
        f"<{tool_name} result compressed for context budget; "
        f"path={path}; offset={offset}{limit_part}; "
        f"re-read with {tool_name}(path=\"{path}\", offset={offset}{limit_part})>"
        if path
        else f"<{tool_name} result compressed for context budget>"
    )
    lines = content.splitlines()
    if not lines:
        return descriptor, "read_empty_placeholder"

    head_count = min(50, max(8, max_chars // 120))
    tail_count = min(30, max(6, max_chars // 180))
    outline_lines = _extract_code_outline_lines(lines, max_items=18)
    if len(lines) <= head_count + tail_count + 12:
        body = "\n".join(lines)
        return f"{descriptor}\n{body}", "read_small_preserve"

    chunks = [descriptor]
    if outline_lines:
        chunks.append("--- code structure/imports retained ---")
        chunks.extend(outline_lines)
    chunks.append(f"--- head: first {head_count} lines of result ---")
    chunks.extend(lines[:head_count])
    omitted = max(0, len(lines) - head_count - tail_count)
    chunks.append(f"...[{omitted} middle lines omitted from read_file-like result]...")
    chunks.append(f"--- tail: last {tail_count} lines of result ---")
    chunks.extend(lines[-tail_count:])
    chunks.append(
        "Instruction: this read result was already available; use the retained "
        "structure/head/tail unless exact omitted lines are required."
    )
    return "\n".join(chunks), "read_head_outline_tail"


def _compress_search_result(
    tool_name: str,
    args: dict[str, Any],
    content: str,
    max_chars: int,
) -> tuple[str, str]:
    pattern = str(args.get("pattern") or "")
    path = str(args.get("path") or ".")
    descriptor = (
        f"<{tool_name} global search result compressed for context budget; "
        f"pattern={pattern!r}; path={path!r}>"
    )
    lines = [line for line in content.splitlines() if line.strip()]
    grouped: dict[str, list[str]] = defaultdict(list)
    unmatched: list[str] = []
    for line in lines:
        parsed = _parse_search_line(line)
        if parsed is None:
            unmatched.append(line)
            continue
        file_path, rest = parsed
        grouped[file_path].append(rest)

    if not grouped:
        body = _head_tail(lines, head=40, tail=20)
        return f"{descriptor}\n" + "\n".join(body), "search_head_tail"

    chunks = [
        descriptor,
        f"matched_files={len(grouped)} total_match_lines={sum(len(v) for v in grouped.values())}",
    ]
    per_file_limit = 4
    for file_path in sorted(grouped)[:80]:
        matches = grouped[file_path]
        chunks.append(f"- {file_path}: {len(matches)} matches")
        for item in matches[:per_file_limit]:
            chunks.append(f"  {item}")
        if len(matches) > per_file_limit:
            chunks.append(f"  ...[{len(matches) - per_file_limit} more matches in this file]")
        if len("\n".join(chunks)) >= max_chars:
            chunks.append("...[additional matched files omitted]")
            break
    if unmatched:
        chunks.append("--- non-standard lines ---")
        chunks.extend(unmatched[:8])
    return "\n".join(chunks), "search_group_by_file"


def _compress_diff_result(
    tool_name: str,
    args: dict[str, Any],
    content: str,
    max_chars: int,
) -> tuple[str, str]:
    path = str(args.get("path") or ".")
    descriptor = f"<{tool_name} diff compressed for context budget; path={path!r}>"
    files = _split_diff_files(content)
    if not files:
        return f"{descriptor}\n" + "\n".join(_head_tail(content.splitlines(), 50, 30)), "diff_head_tail"

    chunks = [descriptor, f"changed_files={len(files)}"]
    for file_header, lines in files:
        chunks.append(file_header)
        for hunk_header, hunk_lines in _split_diff_hunks(lines):
            chunks.append(hunk_header)
            chunks.extend(_compact_diff_hunk(hunk_lines))
            if len("\n".join(chunks)) >= max_chars:
                chunks.append("...[additional diff hunks omitted]")
                return "\n".join(chunks), "diff_hunks_only"
    return "\n".join(chunks), "diff_hunks_only"


def _compress_bash_result(args: dict[str, Any], content: str, max_chars: int) -> tuple[str, str]:
    command = str(args.get("command") or "")
    descriptor = f"<bash result compressed for context budget; command={command!r}>"
    if _looks_like_traceback(content):
        return f"{descriptor}\n" + _compress_traceback(content, max_chars), "bash_traceback_top_bottom"
    if _looks_like_diff(content) or command.strip().startswith(("git diff", "diff ")):
        body, _ = _compress_diff_result("bash", args, content, max_chars)
        return body, "bash_diff_hunks"
    if _looks_like_pytest(content):
        return f"{descriptor}\n" + _compress_pytest(content, max_chars), "bash_pytest_failures"
    if command.strip().startswith(("rg ", "grep ")):
        body, _ = _compress_search_result("bash", {"pattern": command, "path": "."}, content, max_chars)
        return body, "bash_search_group_by_file"
    return f"{descriptor}\n" + "\n".join(_head_tail(content.splitlines(), 60, 35)), "bash_head_tail"


def _compress_global_context_result(
    tool_name: str,
    args: dict[str, Any],
    content: str,
    max_chars: int,
) -> tuple[str, str]:
    descriptor = _legacy_global_descriptor(tool_name, args)
    lines = content.splitlines()
    body = _head_tail(lines, head=80, tail=40)
    return f"{descriptor}\n" + "\n".join(body), "global_context_head_tail"


def _compress_unknown_result(
    tool_name: str,
    content: str,
    max_chars: int,
    *,
    ref: dict[str, Any] | None,
) -> tuple[str, str]:
    placeholder = f"<{tool_name} result compressed for context budget>"
    if not ref:
        return placeholder, "unknown_legacy_placeholder"
    lines = content.splitlines()
    return f"{placeholder}\n" + "\n".join(_head_tail(lines, 50, 30)), "unknown_head_tail_with_ref"


def _legacy_global_descriptor(tool_name: str, args: dict[str, Any]) -> str:
    if tool_name in {"list_files", "storage_list_files", "sandbox_list_files"}:
        path = str(args.get("path") or ".")
        offset = args.get("offset", 0)
        recursive = args.get("recursive")
        recursive_part = f", recursive={bool(recursive)}" if recursive is not None else ""
        return (
            f"<{tool_name} result compressed for context budget; "
            f"path={path}; offset={offset}; "
            f"re-list with {tool_name}(path=\"{path}\"{recursive_part}, offset={offset})>"
        )
    if tool_name == "repo_map":
        path = str(args.get("path") or ".")
        offset = args.get("offset", 0)
        max_depth = args.get("max_depth")
        include_lines = args.get("include_lines")
        max_depth_part = f", max_depth={max_depth}" if max_depth not in (None, "") else ""
        include_part = (
            f", include_lines={bool(include_lines)}"
            if include_lines is not None
            else ""
        )
        return (
            f"<repo_map result compressed for context budget; "
            f"path={path}; offset={offset}{max_depth_part}{include_part}; "
            f"re-map with repo_map(path=\"{path}\"{max_depth_part}{include_part}, offset={offset})>"
        )
    if tool_name == "code_outline":
        path = str(args.get("path") or "")
        offset = args.get("offset", 0)
        limit = args.get("limit")
        limit_part = f", limit={limit}" if limit not in (None, "") else ""
        if path:
            return (
                f"<code_outline result compressed for context budget; "
                f"path={path}; offset={offset}{limit_part}; "
                f"re-outline with code_outline(path=\"{path}\", offset={offset}{limit_part})>"
            )
    return (
        f"<{tool_name} global context result compressed for context budget; "
        f"arguments={_json_preview(args, 300)}>"
    )


def _compress_traceback(content: str, max_chars: int) -> str:
    lines = content.splitlines()
    if len(lines) <= 80:
        return content
    head = lines[:14]
    tail = lines[-28:]
    omitted = len(lines) - len(head) - len(tail)
    return "\n".join([
        *head,
        f"...[{omitted} lines of internal traceback omitted]...",
        *tail,
    ])


def _compress_pytest(content: str, max_chars: int) -> str:
    lines = content.splitlines()
    important = [
        line for line in lines
        if (
            line.startswith("FAILED ")
            or line.startswith("ERROR ")
            or " short test summary info " in line
            or re.search(r"\b(?:AssertionError|E\s+AssertionError|Traceback)\b", line)
        )
    ]
    chunks = []
    if important:
        chunks.append("--- pytest important lines ---")
        chunks.extend(important[:80])
    chunks.append("--- pytest tail ---")
    chunks.extend(lines[-80:])
    return _limit_text("\n".join(chunks), max_chars)


def _extract_code_outline_lines(lines: list[str], *, max_items: int) -> list[str]:
    selected: list[str] = []
    patterns = [
        re.compile(r"^\s*(?:from\s+\S+\s+import\s+.+|import\s+.+)$"),
        re.compile(r"^\s*(?:class|def)\s+[A-Za-z_]\w*"),
        re.compile(r"^\s*async\s+def\s+[A-Za-z_]\w*"),
        re.compile(r"^\s*(?:export\s+)?(?:class|function)\s+[A-Za-z_$][\w$]*"),
        re.compile(r"^\s*(?:export\s+)?const\s+[A-Za-z_$][\w$]*\s*="),
    ]
    for index, line in enumerate(lines, 1):
        if any(pattern.search(line) for pattern in patterns):
            selected.append(f"{index}: {line}")
        if len(selected) >= max_items:
            break
    return selected


def _split_diff_files(content: str) -> list[tuple[str, list[str]]]:
    files: list[tuple[str, list[str]]] = []
    current_header = ""
    current_lines: list[str] = []
    for line in content.splitlines():
        if line.startswith("diff --git "):
            if current_header or current_lines:
                files.append((current_header or "diff file", current_lines))
            current_header = line
            current_lines = []
        else:
            current_lines.append(line)
    if current_header or current_lines:
        files.append((current_header or "diff file", current_lines))
    return files


def _split_diff_hunks(lines: list[str]) -> list[tuple[str, list[str]]]:
    hunks: list[tuple[str, list[str]]] = []
    prelude: list[str] = []
    current_header = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("@@ "):
            if current_header:
                hunks.append((current_header, current_lines))
            elif prelude:
                hunks.append(("--- file metadata ---", prelude))
            current_header = line
            current_lines = []
        elif current_header:
            current_lines.append(line)
        else:
            prelude.append(line)
    if current_header:
        hunks.append((current_header, current_lines))
    elif prelude:
        hunks.append(("--- file metadata ---", prelude))
    return hunks


def _compact_diff_hunk(lines: list[str]) -> list[str]:
    changed = [line for line in lines if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))]
    if len(lines) <= 80:
        return lines
    compacted: list[str] = []
    context_buffer: list[str] = []
    for line in lines:
        if line.startswith(("+++", "---")):
            compacted.append(line)
            continue
        if line.startswith(("+", "-")):
            if context_buffer:
                compacted.extend(_edge_context(context_buffer))
                context_buffer = []
            compacted.append(line)
        else:
            context_buffer.append(line)
    if context_buffer:
        compacted.extend(_edge_context(context_buffer))
    if not changed:
        return _head_tail(lines, 30, 20)
    return compacted


def _edge_context(lines: list[str]) -> list[str]:
    if len(lines) <= 6:
        return lines
    return [*lines[:3], f" ...[{len(lines) - 6} unchanged context lines omitted]...", *lines[-3:]]


def _head_tail(lines: list[str], head: int, tail: int) -> list[str]:
    if len(lines) <= head + tail + 3:
        return lines
    omitted = len(lines) - head - tail
    return [*lines[:head], f"...[{omitted} middle lines omitted]...", *lines[-tail:]]


def _parse_search_line(line: str) -> tuple[str, str] | None:
    match = re.match(r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::(?P<column>\d+))?:(?P<body>.*)$", line)
    if not match:
        return None
    location = match.group("line")
    if match.group("column"):
        location += f":{match.group('column')}"
    return match.group("path"), f"{location}: {match.group('body')}"


def _message_arguments(message: dict[str, Any]) -> dict[str, Any]:
    args = message.get("final_arguments")
    if isinstance(args, dict):
        return args
    return {}


def _with_retrieval_hint(content: str, ref: dict[str, Any]) -> str:
    result_id = str(ref.get("result_id") or "")
    if not result_id:
        return content
    hint = (
        f"Original full tool result stored: tool_result://{result_id}. "
        f"Use retrieve_tool_result(result_id=\"{result_id}\", offset=0, limit=12000) "
        "only if exact omitted text is needed."
    )
    return f"{content}\n{hint}"


def _looks_like_traceback(content: str) -> bool:
    return "Traceback (most recent call last):" in content or re.search(r"\n\s*File \".+\", line \d+", content) is not None


def _looks_like_pytest(content: str) -> bool:
    return (
        " short test summary info " in content
        or re.search(r"^FAILED .+::", content, flags=re.MULTILINE) is not None
        or "pytest" in content.lower() and "AssertionError" in content
    )


def _looks_like_diff(content: str) -> bool:
    return (
        content.startswith("diff --git ")
        or "\ndiff --git " in content
        or re.search(r"^@@ -\d+", content, flags=re.MULTILINE) is not None
    )


def _json_preview(value: Any, max_chars: int) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        text = str(value)
    return _limit_text(text, max_chars, marker="...")


def _limit_text(text: str, max_chars: int, *, marker: str = "\n...[truncated]") -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker) + 20:
        return text[:max_chars]
    return text[: max_chars - len(marker)].rstrip() + marker


def _limit_text_middle(text: str, max_chars: int, *, marker: str) -> str:
    text = str(text or "")
    if len(text) <= max_chars:
        return text
    if max_chars <= len(marker) + 40:
        return text[:max_chars]
    keep = max_chars - len(marker)
    head = max(20, keep // 2)
    tail = max(20, keep - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()
