from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from typing import Any

from user_scope import storage_root_for_user


TEXT_EXTENSIONS = {
    ".txt", ".md", ".py", ".js", ".css", ".html", ".json", ".csv", ".log", ".yaml", ".yml",
}


def storage_help_text() -> str:
    return (
        "Storage 命令：\n"
        "/files [目录] - 列出你的 storage 文件\n"
        "/cat <文件> - 预览文本文件\n"
        "/download <文件> - 下载文件\n"
        "路径均相对于你的私有 storage。"
    )


def list_storage_text(user_id: str, relative_path: str = "") -> str:
    root = _storage_root(user_id)
    current = _safe_storage_path(user_id, relative_path)
    if not current.exists():
        return f"路径不存在：{_clean_path(relative_path) or '/'}"
    if not current.is_dir():
        return f"这不是目录：{_display_path(current, root)}"

    entries = []
    for path in current.iterdir():
        if path.name.startswith(".") or path.is_symlink():
            continue
        resolved = path.resolve()
        if resolved != root and not resolved.is_relative_to(root):
            continue
        entries.append(path)
    entries.sort(key=lambda item: (not item.is_dir(), item.name.lower()))

    rel = _display_path(current, root) or "/"
    if not entries:
        return f"storage/{rel}\n\n空目录。"

    lines = [f"storage/{rel}", ""]
    parent = _display_path(current.parent, root) if current != root else ""
    if parent:
        lines.append(f"[dir] ..  /files {parent}")
    for path in entries[:50]:
        display = _display_path(path, root)
        if path.is_dir():
            lines.append(f"[dir] {path.name}/  /files {display}")
        else:
            lines.append(
                f"[file] {path.name} ({_format_size(path.stat().st_size)})  "
                f"/cat {display}"
            )
    if len(entries) > 50:
        lines.append(f"... 还有 {len(entries) - 50} 个项目未显示。")
    return "\n".join(lines)


def preview_storage_text(
    user_id: str,
    relative_path: str,
    *,
    max_bytes: int | None = None,
) -> str:
    path = _safe_storage_path(user_id, relative_path)
    root = _storage_root(user_id)
    if not path.exists():
        return f"文件不存在：{_clean_path(relative_path)}"
    if not path.is_file():
        return f"这不是文件：{_display_path(path, root)}"
    if not _is_text_file(path):
        return "这个文件不像文本文件。可以用 /download 下载。"
    limit = max(1, int(max_bytes or _env_int("TELEGRAM_STORAGE_PREVIEW_BYTES", 8000)))
    content = path.read_bytes()[: limit + 1]
    truncated = len(content) > limit
    if truncated:
        content = content[:limit]
    text = content.decode("utf-8", errors="replace")
    suffix = "\n\n[内容已截断，使用 /download 获取完整文件。]" if truncated else ""
    return f"storage/{_display_path(path, root)}\n\n{text}{suffix}"


def resolve_download_path(
    user_id: str,
    relative_path: str,
    *,
    max_bytes: int | None = None,
) -> Path:
    path = _safe_storage_path(user_id, relative_path)
    limit = max_bytes or _env_int("TELEGRAM_STORAGE_DOWNLOAD_MAX_BYTES", 10 * 1024 * 1024)
    if not path.exists():
        raise FileNotFoundError("文件不存在。")
    if not path.is_file():
        raise IsADirectoryError("目录不能下载。")
    if path.stat().st_size > limit:
        raise ValueError(f"文件超过下载限制：{_format_size(limit)}。")
    return path


def _storage_root(user_id: str) -> Path:
    root = storage_root_for_user(Path.cwd(), user_id).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _safe_storage_path(user_id: str, relative_path: str | None) -> Path:
    root = _storage_root(user_id)
    raw = _clean_path(relative_path)
    candidate = (root / raw).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise ValueError("路径越过了 storage 边界。")
    if candidate.is_symlink():
        raise ValueError("不允许访问符号链接。")
    return candidate


def _clean_path(value: str | None) -> str:
    return str(value or "").strip().lstrip("/")


def _display_path(path: Path, root: Path) -> str:
    if path == root:
        return ""
    return path.relative_to(root).as_posix()


def _is_text_file(path: Path) -> bool:
    mime = mimetypes.guess_type(path.name)[0] or ""
    if mime.startswith("text/") or mime in {
        "application/json",
        "application/xml",
        "application/javascript",
    }:
        return True
    return path.suffix.lower() in TEXT_EXTENSIONS


def _format_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default
