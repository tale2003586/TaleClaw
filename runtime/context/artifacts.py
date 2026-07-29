"""Content-addressed persistence for context artifacts.

An artifact is intentionally independent of a session.  Session events and
task state retain an :class:`ArtifactRef`, while this store owns the original
bytes exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Mapping


_ARTIFACT_PREFIX = "art_"


@dataclass(frozen=True)
class ArtifactRef:
    """Small, serializable reference suitable for event and task-state data."""

    artifact_id: str
    artifact_type: str
    name: str
    mime_type: str
    size_bytes: int
    size_chars: int
    content_hash: str
    storage_uri: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "artifact_type": self.artifact_type,
            "name": self.name,
            "mime_type": self.mime_type,
            "size_bytes": self.size_bytes,
            "size_chars": self.size_chars,
            "content_hash": self.content_hash,
            "storage_uri": self.storage_uri,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            artifact_id=str(value["artifact_id"]),
            artifact_type=str(value.get("artifact_type") or "text"),
            name=str(value.get("name") or "artifact"),
            mime_type=str(value.get("mime_type") or "application/octet-stream"),
            size_bytes=int(value.get("size_bytes") or 0),
            size_chars=int(value.get("size_chars") or 0),
            content_hash=str(value.get("content_hash") or ""),
            storage_uri=str(value.get("storage_uri") or ""),
        )


@dataclass(frozen=True)
class ArtifactMetadata:
    """Persistent artifact description; content itself is never embedded here."""

    artifact_id: str
    artifact_type: str
    name: str
    mime_type: str
    size_bytes: int
    size_chars: int
    content_hash: str
    storage_uri: str
    parse_status: str
    created_at: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def ref(self) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            artifact_type=self.artifact_type,
            name=self.name,
            mime_type=self.mime_type,
            size_bytes=self.size_bytes,
            size_chars=self.size_chars,
            content_hash=self.content_hash,
            storage_uri=self.storage_uri,
        )

    def to_dict(self) -> dict[str, Any]:
        data = self.ref.to_dict()
        data.update({
            "parse_status": self.parse_status,
            "created_at": self.created_at,
            "metadata": self.metadata,
        })
        return data

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactMetadata":
        ref = ArtifactRef.from_dict(value)
        raw_metadata = value.get("metadata")
        return cls(
            **ref.to_dict(),
            parse_status=str(value.get("parse_status") or "ready"),
            created_at=str(value.get("created_at") or ""),
            metadata=dict(raw_metadata) if isinstance(raw_metadata, Mapping) else {},
        )


class ArtifactNotFoundError(KeyError):
    """Raised when an artifact reference cannot be resolved by a store."""


class ArtifactStore:
    """Filesystem-backed, content-addressed artifact storage.

    Content is placed under ``content/<sha256>`` and metadata under
    ``metadata/<artifact_id>.json``.  The content file is published before
    metadata, so a failed metadata write cannot expose partial metadata.
    Orphaned content is harmless and may be collected separately.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self._content_dir = self.root / "content"
        self._metadata_dir = self.root / "metadata"
        self._content_dir.mkdir(parents=True, exist_ok=True)
        self._metadata_dir.mkdir(parents=True, exist_ok=True)

    def put_artifact(
        self,
        content: str | bytes,
        *,
        artifact_type: str = "text",
        name: str | None = None,
        mime_type: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        parse_status: str = "ready",
    ) -> ArtifactRef:
        """Persist content once and return its compact reference.

        Repeated writes with identical bytes return the original immutable
        metadata/ref rather than duplicating content or silently changing its
        provenance fields.
        """
        raw = _as_bytes(content)
        content_hash = hashlib.sha256(raw).hexdigest()
        artifact_id = _ARTIFACT_PREFIX + content_hash
        content_path = self._content_dir / content_hash
        metadata_path = self._metadata_dir / f"{artifact_id}.json"

        existing = self._load_metadata_path(metadata_path)
        if existing is not None:
            return existing.ref

        self._atomic_write_if_missing(content_path, raw)
        created_at = datetime.now(timezone.utc).isoformat()
        text = raw.decode("utf-8", errors="replace")
        record = ArtifactMetadata(
            artifact_id=artifact_id,
            artifact_type=str(artifact_type or "text"),
            name=str(name or f"{artifact_type or 'artifact'}-{content_hash[:12]}"),
            mime_type=str(mime_type or _default_mime_type(artifact_type)),
            size_bytes=len(raw),
            size_chars=len(text),
            content_hash=content_hash,
            storage_uri=f"artifact://{artifact_id}",
            parse_status=str(parse_status or "ready"),
            created_at=created_at,
            metadata=_json_safe_mapping(metadata),
        )
        # Metadata publication is last.  If it fails, callers receive an error
        # and no malformed/partial metadata record exists.
        self._atomic_write_if_missing(
            metadata_path,
            json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"),
        )
        persisted = self._load_metadata_path(metadata_path)
        if persisted is None:  # Defensive: should only occur on an external race.
            raise OSError(f"artifact metadata was not published: {artifact_id}")
        return persisted.ref

    put = put_artifact

    def get_artifact_metadata(self, artifact: str | ArtifactRef | ArtifactMetadata) -> ArtifactMetadata:
        artifact_id = _artifact_id(artifact)
        record = self._load_metadata_path(self._metadata_dir / f"{artifact_id}.json")
        if record is None:
            raise ArtifactNotFoundError(artifact_id)
        return record

    get_metadata = get_artifact_metadata

    def read_artifact(
        self,
        artifact: str | ArtifactRef | ArtifactMetadata,
        *,
        as_bytes: bool = False,
    ) -> str | bytes:
        record = self.get_artifact_metadata(artifact)
        path = self._content_dir / record.content_hash
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise ArtifactNotFoundError(record.artifact_id) from exc
        if hashlib.sha256(raw).hexdigest() != record.content_hash:
            raise OSError(f"artifact content hash mismatch: {record.artifact_id}")
        return raw if as_bytes else raw.decode("utf-8", errors="replace")

    read = read_artifact

    def read_artifact_range(
        self,
        artifact: str | ArtifactRef | ArtifactMetadata,
        start: int = 0,
        end: int | None = None,
        *,
        as_bytes: bool = False,
    ) -> str | bytes:
        """Read a half-open character range (or byte range with ``as_bytes``)."""
        if start < 0 or (end is not None and end < start):
            raise ValueError("artifact ranges must satisfy 0 <= start <= end")
        data = self.read_artifact(artifact, as_bytes=as_bytes)
        return data[start:end]  # type: ignore[index]

    def search_artifact(
        self,
        artifact: str | ArtifactRef | ArtifactMetadata,
        query: str,
        *,
        max_results: int = 20,
        case_sensitive: bool = False,
        context_chars: int = 120,
    ) -> list[dict[str, Any]]:
        """Return deterministic text matches with offsets and compact snippets."""
        if max_results <= 0 or not query:
            return []
        text = self.read_artifact(artifact)
        assert isinstance(text, str)
        needle = query if case_sensitive else query.casefold()
        haystack = text if case_sensitive else text.casefold()
        matches: list[dict[str, Any]] = []
        offset = 0
        while len(matches) < max_results:
            index = haystack.find(needle, offset)
            if index < 0:
                break
            line_start = text.rfind("\n", 0, index) + 1
            line_end = text.find("\n", index)
            if line_end < 0:
                line_end = len(text)
            start = max(0, index - max(0, context_chars))
            end = min(len(text), index + len(query) + max(0, context_chars))
            matches.append({
                "start": index,
                "end": index + len(query),
                "line": text.count("\n", 0, index) + 1,
                "snippet": text[start:end],
                "line_text": text[line_start:line_end],
            })
            offset = index + max(1, len(query))
        return matches

    search = search_artifact

    def get_artifact_outline(
        self,
        artifact: str | ArtifactRef | ArtifactMetadata,
        *,
        max_items: int = 200,
    ) -> list[dict[str, Any]]:
        """Extract lightweight headings/key structure without copying the body."""
        text = self.read_artifact(artifact)
        assert isinstance(text, str)
        items: list[dict[str, Any]] = []
        for line_number, line in enumerate(text.splitlines(), start=1):
            stripped = line.strip()
            level = 0
            title = ""
            if stripped.startswith("#"):
                level = len(stripped) - len(stripped.lstrip("#"))
                title = stripped[level:].strip()
            elif stripped and not line[:1].isspace() and stripped.endswith(":"):
                level, title = 1, stripped[:-1].strip()
            if title:
                items.append({"line": line_number, "level": level, "title": title})
                if len(items) >= max_items:
                    break
        if items:
            return items
        # JSON is common enough to merit a useful no-parser outline.
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return []
        if isinstance(parsed, Mapping):
            return [
                {"line": None, "level": 1, "title": str(key), "kind": type(value).__name__}
                for key, value in list(parsed.items())[:max_items]
            ]
        if isinstance(parsed, list):
            return [{"line": None, "level": 1, "title": f"items: {len(parsed)}", "kind": "list"}]
        return []

    outline = get_artifact_outline

    def sample_artifact(
        self,
        artifact: str | ArtifactRef | ArtifactMetadata,
        *,
        max_chars: int = 1000,
        head_chars: int | None = None,
        tail_chars: int | None = None,
    ) -> str:
        """Return a bounded head/tail sample, clearly marking an omitted middle."""
        text = self.read_artifact(artifact)
        assert isinstance(text, str)
        limit = max(0, int(max_chars))
        if len(text) <= limit:
            return text
        marker = "\n...[artifact sample truncated]...\n"
        if limit <= len(marker):
            return text[:limit]
        remaining = limit - len(marker)
        head = min(remaining, head_chars) if head_chars is not None else remaining // 2
        tail = min(remaining - head, tail_chars) if tail_chars is not None else remaining - head
        # Fill unused budget where a caller constrained only one side.
        head = min(remaining - tail, head)
        tail = remaining - head
        return text[:head].rstrip() + marker + text[-tail:].lstrip()

    sample = sample_artifact

    def _atomic_write_if_missing(self, path: Path, data: bytes) -> None:
        if path.exists():
            return
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_name, path)
            except FileExistsError:
                pass
            except OSError:
                # ``os.replace`` is atomic but may replace a concurrent writer.
                # All concurrent writers use the same content-addressed value,
                # so replacement is safe for content and equivalent metadata.
                if not path.exists():
                    os.replace(temporary_name, path)
                    temporary_name = ""
        finally:
            if temporary_name:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass

    @staticmethod
    def _load_metadata_path(path: Path) -> ArtifactMetadata | None:
        try:
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise OSError(f"invalid artifact metadata: {path}") from exc
        if not isinstance(payload, Mapping):
            raise OSError(f"invalid artifact metadata: {path}")
        return ArtifactMetadata.from_dict(payload)


def _as_bytes(content: str | bytes) -> bytes:
    if isinstance(content, bytes):
        return content
    if isinstance(content, str):
        return content.encode("utf-8")
    raise TypeError("artifact content must be str or bytes")


def _artifact_id(value: str | ArtifactRef | ArtifactMetadata) -> str:
    if isinstance(value, (ArtifactRef, ArtifactMetadata)):
        return value.artifact_id
    raw = str(value)
    if raw.startswith("artifact://"):
        raw = raw[len("artifact://"):]
    if not raw.startswith(_ARTIFACT_PREFIX) or "/" in raw or "\\" in raw:
        raise ArtifactNotFoundError(raw)
    return raw


def _default_mime_type(artifact_type: str) -> str:
    return {
        "text": "text/plain",
        "json": "application/json",
        "log": "text/plain",
        "tool_result": "text/plain",
        "code": "text/plain",
    }.get(str(artifact_type), "application/octet-stream")


def _json_safe_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    # Round-trip through json prevents a mutable caller-owned object from
    # becoming persistent state and guarantees metadata can be written.
    return json.loads(json.dumps(dict(value), ensure_ascii=False, default=str))
