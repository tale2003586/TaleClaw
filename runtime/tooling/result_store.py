"""Persistent and file-backed tool result stores."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from config import ARTIFACT_OFFLOADING_ENABLED, CONTEXT_ARTIFACT_ROOT, WORKDIR
from runtime.context.artifacts import ArtifactStore
from runtime.db import connect, resolve_database_config


DEFAULT_TOOL_RESULT_ROOT = WORKDIR / ".tool_results"
TOOL_RESULT_STORE_REF_KEY = "tool_result_ref"


@dataclass(frozen=True)
class StoredToolResult:
    result_id: str
    backend: str
    uri: str
    chars: int
    sha256: str
    metadata: dict[str, Any]


class ToolResultStore:
    backend = "base"

    def put(
        self,
        *,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        content: str,
        status: str,
    ) -> StoredToolResult:
        raise NotImplementedError

    def get(self, result_id: str) -> tuple[str, dict[str, Any]]:
        raise NotImplementedError


class ArtifactToolResultStore(ToolResultStore):
    """Unified backend for new tool results; legacy stores remain readable."""

    backend = "artifact"

    def __init__(self, root: str | Path | None = None) -> None:
        self.store = ArtifactStore(
            root or os.getenv("CONTEXT_ARTIFACT_ROOT") or CONTEXT_ARTIFACT_ROOT
        )

    def put(
        self,
        *,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        content: str,
        status: str,
    ) -> StoredToolResult:
        text = str(content or "")
        ref = self.store.put_artifact(
            text,
            artifact_type="tool_result",
            name=f"{tool_name or 'tool'}-{call_id or 'result'}",
            mime_type="text/plain",
            metadata={
                "session_id": str(session_id or ""),
                "call_id": str(call_id or ""),
                "tool_name": str(tool_name or ""),
                "arguments": arguments or {},
                "status": str(status or ""),
            },
        )
        metadata = self.store.get_artifact_metadata(ref).to_dict()
        metadata.update({
            "result_id": ref.artifact_id,
            "backend": self.backend,
            "uri": ref.storage_uri,
            "chars": ref.size_chars,
            "sha256": ref.content_hash,
            "tool_name": str(tool_name or ""),
            "call_id": str(call_id or ""),
        })
        return StoredToolResult(
            result_id=ref.artifact_id,
            backend=self.backend,
            uri=ref.storage_uri,
            chars=ref.size_chars,
            sha256=ref.content_hash,
            metadata=metadata,
        )

    def get(self, result_id: str) -> tuple[str, dict[str, Any]]:
        artifact_id = _clean_result_id(result_id)
        metadata = self.store.get_artifact_metadata(artifact_id)
        content = self.store.read_artifact(metadata)
        assert isinstance(content, str)
        result = metadata.to_dict()
        result.update({
            "result_id": metadata.artifact_id,
            "backend": self.backend,
            "uri": metadata.storage_uri,
            "chars": metadata.size_chars,
            "sha256": metadata.content_hash,
            **dict(metadata.metadata or {}),
        })
        return content, result


class FileToolResultStore(ToolResultStore):
    backend = "file"

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root or os.getenv("TOOL_RESULT_STORE_ROOT") or DEFAULT_TOOL_RESULT_ROOT)

    def put(
        self,
        *,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        content: str,
        status: str,
    ) -> StoredToolResult:
        text = str(content or "")
        result_id = f"tr_{uuid4().hex}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        self.root.mkdir(parents=True, exist_ok=True)
        content_path = self.root / f"{result_id}.txt"
        metadata_path = self.root / f"{result_id}.json"
        metadata = {
            "result_id": result_id,
            "backend": self.backend,
            "uri": f"tool_result://{result_id}",
            "session_id": str(session_id or ""),
            "call_id": str(call_id or ""),
            "tool_name": str(tool_name or ""),
            "arguments": arguments or {},
            "status": str(status or ""),
            "chars": len(text),
            "sha256": digest,
            "created_at": created_at,
            "content_path": str(content_path),
        }
        content_path.write_text(text, encoding="utf-8")
        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return StoredToolResult(
            result_id=result_id,
            backend=self.backend,
            uri=metadata["uri"],
            chars=len(text),
            sha256=digest,
            metadata=metadata,
        )

    def get(self, result_id: str) -> tuple[str, dict[str, Any]]:
        result_id = _clean_result_id(result_id)
        metadata_path = self.root / f"{result_id}.json"
        content_path = self.root / f"{result_id}.txt"
        if not metadata_path.exists() or not content_path.exists():
            raise FileNotFoundError(f"Tool result not found: {result_id}")
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        return content_path.read_text(encoding="utf-8"), metadata


class PostgresToolResultStore(ToolResultStore):
    backend = "postgres"

    def __init__(self, dsn: str | None = None) -> None:
        self.config = resolve_database_config(
            dsn,
            env_names=("TOOL_RESULT_DATABASE_URL", "DATABASE_URL"),
            purpose="tool result store",
        )
        self._ensure_schema()

    def put(
        self,
        *,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        content: str,
        status: str,
    ) -> StoredToolResult:
        text = str(content or "")
        result_id = f"tr_{uuid4().hex}"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {
            "result_id": result_id,
            "backend": self.backend,
            "uri": f"tool_result://{result_id}",
            "session_id": str(session_id or ""),
            "call_id": str(call_id or ""),
            "tool_name": str(tool_name or ""),
            "arguments": arguments or {},
            "status": str(status or ""),
            "chars": len(text),
            "sha256": digest,
            "created_at": created_at,
        }
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO tool_results (
                        result_id, session_id, call_id, tool_name,
                        arguments_json, status, content, content_sha256,
                        created_at, metadata_json
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        result_id,
                        str(session_id or ""),
                        str(call_id or ""),
                        str(tool_name or ""),
                        json.dumps(arguments or {}, ensure_ascii=False),
                        str(status or ""),
                        text,
                        digest,
                        created_at,
                        json.dumps(metadata, ensure_ascii=False),
                    ),
                )
                conn.commit()
        return StoredToolResult(
            result_id=result_id,
            backend=self.backend,
            uri=metadata["uri"],
            chars=len(text),
            sha256=digest,
            metadata=metadata,
        )

    def get(self, result_id: str) -> tuple[str, dict[str, Any]]:
        result_id = _clean_result_id(result_id)
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT content, metadata_json FROM tool_results WHERE result_id = %s",
                    (result_id,),
                )
                row = cursor.fetchone()
        if not row:
            raise FileNotFoundError(f"Tool result not found: {result_id}")
        metadata = row["metadata_json"]
        if isinstance(metadata, str):
            metadata = json.loads(metadata)
        return str(row["content"] or ""), dict(metadata or {})

    def _ensure_schema(self) -> None:
        with connect(self.config) as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tool_results (
                        result_id TEXT PRIMARY KEY,
                        session_id TEXT NOT NULL,
                        call_id TEXT NOT NULL,
                        tool_name TEXT NOT NULL,
                        arguments_json TEXT NOT NULL,
                        status TEXT NOT NULL,
                        content TEXT NOT NULL,
                        content_sha256 TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        metadata_json TEXT NOT NULL
                    )
                    """
                )
                cursor.execute(
                    "CREATE INDEX IF NOT EXISTS idx_tool_results_session "
                    "ON tool_results(session_id, created_at DESC)"
                )
                conn.commit()


def resolve_tool_result_store() -> ToolResultStore:
    backend = str(os.getenv("TOOL_RESULT_STORE_BACKEND") or "artifact").strip().lower()
    if backend in {"postgres", "postgresql", "pg"}:
        return PostgresToolResultStore()
    if backend in {"file", "legacy-file"}:
        return FileToolResultStore()
    if ARTIFACT_OFFLOADING_ENABLED:
        return ArtifactToolResultStore()
    return FileToolResultStore()


def store_tool_result(
    *,
    session_id: str,
    call_id: str,
    tool_name: str,
    arguments: dict[str, Any],
    content: str,
    status: str,
) -> StoredToolResult:
    return resolve_tool_result_store().put(
        session_id=session_id,
        call_id=call_id,
        tool_name=tool_name,
        arguments=arguments,
        content=content,
        status=status,
    )


def retrieve_tool_result(
    result_id: str,
    *,
    offset: int = 0,
    limit: int | None = None,
    query: str | None = None,
) -> str:
    content, metadata = resolve_tool_result_store().get(result_id)
    limit = _bounded_limit(limit)
    if query:
        body = _query_windows(content, query, limit)
        mode = f"query={query!r}"
    else:
        offset = max(0, int(offset or 0))
        body = content[offset : offset + limit]
        mode = f"offset={offset}, limit={limit}"
    header = (
        f"[retrieve_tool_result] {metadata.get('result_id') or result_id} "
        f"tool={metadata.get('tool_name') or 'unknown'} "
        f"chars={len(content)} {mode}"
    )
    return f"{header}\n{body}"


def tool_result_ref_from_metadata(metadata: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(metadata, dict):
        return None
    ref = metadata.get(TOOL_RESULT_STORE_REF_KEY)
    return ref if isinstance(ref, dict) else None


def _clean_result_id(value: str) -> str:
    result_id = str(value or "").strip()
    if result_id.startswith("tool_result://"):
        result_id = result_id.removeprefix("tool_result://")
    if result_id.startswith("artifact://"):
        result_id = result_id.removeprefix("artifact://")
    if not result_id or "/" in result_id or "\\" in result_id or ".." in result_id:
        raise ValueError(f"Invalid tool result id: {value}")
    return result_id


def _bounded_limit(limit: int | None) -> int:
    try:
        value = int(limit) if limit is not None else 12000
    except (TypeError, ValueError):
        value = 12000
    return max(200, min(value, 50000))


def _query_windows(content: str, query: str, limit: int) -> str:
    needle = str(query or "").lower()
    if not needle:
        return content[:limit]
    lines = content.splitlines()
    selected: list[str] = []
    used_indexes: set[int] = set()
    for index, line in enumerate(lines):
        if needle not in line.lower():
            continue
        for item in range(max(0, index - 2), min(len(lines), index + 3)):
            if item in used_indexes:
                continue
            used_indexes.add(item)
            selected.append(f"{item + 1}: {lines[item]}")
        if len("\n".join(selected)) >= limit:
            break
    if not selected:
        return f"No matches for query {query!r}."
    body = "\n".join(selected)
    if len(body) > limit:
        body = body[:limit].rstrip() + "\n...[retrieve_tool_result query output truncated]"
    return body
