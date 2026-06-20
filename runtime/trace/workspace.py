from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runs",
    ".sessions",
    ".task_sessions",
    ".tasks",
    ".team",
    ".transcripts",
    "__pycache__",
    "node_modules",
}
DEFAULT_EXCLUDED_SUFFIXES = {
    ".pyc",
    ".pyo",
    ".db",
}
DEFAULT_MAX_HASH_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: str
    files: dict[str, dict[str, Any]]
    skipped: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "file_count": len(self.files),
            "files": [
                {"path": path, **metadata}
                for path, metadata in sorted(self.files.items())
            ],
            "skipped": self.skipped,
        }


def capture_workspace_snapshot(
    root: str | Path,
    *,
    excluded_dirs: set[str] | None = None,
    excluded_suffixes: set[str] | None = None,
    max_hash_bytes: int = DEFAULT_MAX_HASH_BYTES,
) -> WorkspaceSnapshot:
    root_path = Path(root).resolve()
    excluded_dirs = set(excluded_dirs or DEFAULT_EXCLUDED_DIRS)
    excluded_suffixes = set(excluded_suffixes or DEFAULT_EXCLUDED_SUFFIXES)
    files: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []

    if not root_path.exists():
        return WorkspaceSnapshot(root=str(root_path), files={}, skipped=[{
            "path": ".",
            "reason": "root_missing",
        }])

    for current, dirs, filenames in os.walk(root_path):
        current_path = Path(current)
        dirs[:] = [
            name for name in dirs
            if name not in excluded_dirs
            and not _is_hidden_generated_dir(name)
        ]
        for filename in filenames:
            path = current_path / filename
            try:
                relative = path.relative_to(root_path).as_posix()
            except ValueError:
                continue
            if path.suffix in excluded_suffixes:
                skipped.append({"path": relative, "reason": "excluded_suffix"})
                continue
            try:
                stat = path.stat()
            except OSError as exc:
                skipped.append({
                    "path": relative,
                    "reason": "stat_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue
            if not path.is_file():
                continue
            digest = None
            if stat.st_size <= max_hash_bytes:
                try:
                    digest = _sha256(path)
                except OSError as exc:
                    skipped.append({
                        "path": relative,
                        "reason": "hash_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    continue
            else:
                skipped.append({
                    "path": relative,
                    "reason": "too_large_to_hash",
                    "size": stat.st_size,
                })
            files[relative] = {
                "sha256": digest,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return WorkspaceSnapshot(root=str(root_path), files=files, skipped=skipped)


def diff_workspace_snapshots(
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> dict[str, Any]:
    before_paths = set(before.files)
    after_paths = set(after.files)
    created = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = []

    for path in sorted(before_paths & after_paths):
        before_meta = before.files[path]
        after_meta = after.files[path]
        if _changed(before_meta, after_meta):
            modified.append({
                "path": path,
                "before": before_meta,
                "after": after_meta,
            })

    return {
        "root": after.root,
        "created": created,
        "modified": modified,
        "deleted": deleted,
        "summary": {
            "created": len(created),
            "modified": len(modified),
            "deleted": len(deleted),
            "before_files": len(before.files),
            "after_files": len(after.files),
            "before_skipped": len(before.skipped),
            "after_skipped": len(after.skipped),
        },
        "skipped": {
            "before": before.skipped,
            "after": after.skipped,
        },
    }


def write_workspace_artifacts(
    run_dir: Path,
    *,
    before: WorkspaceSnapshot,
    after: WorkspaceSnapshot,
) -> dict[str, Any]:
    diff = diff_workspace_snapshots(before, after)
    _write_json(run_dir / "workspace_snapshot_before.json", before.to_dict())
    _write_json(run_dir / "workspace_snapshot_after.json", after.to_dict())
    _write_json(run_dir / "workspace_diff.json", diff)
    return diff


def _changed(before: dict[str, Any], after: dict[str, Any]) -> bool:
    if before.get("sha256") and after.get("sha256"):
        return before.get("sha256") != after.get("sha256")
    return (
        before.get("size") != after.get("size")
        or before.get("mtime_ns") != after.get("mtime_ns")
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str) + "\n",
        encoding="utf-8",
    )


def _is_hidden_generated_dir(name: str) -> bool:
    return name.startswith(".") and name.endswith("_cache")
