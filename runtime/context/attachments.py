"""Instruction-free rendering of user-supplied attachment metadata."""

from __future__ import annotations

from typing import Any, Mapping


def render_user_attachments_message(session: Any) -> dict[str, Any] | None:
    attachments = latest_user_attachments(session)
    if not attachments:
        return None
    lines = ['<user-attachments source="runtime-generated" instructions="false">']
    for item in attachments:
        ref = item.get("artifact_ref")
        if isinstance(ref, Mapping):
            ref = ref.get("storage_uri") or ref.get("artifact_id")
        attributes = {
            "name": item.get("name") or "attachment",
            "media_type": item.get("media_type") or item.get("mime_type") or "application/octet-stream",
            "size_bytes": max(0, int(item.get("size_bytes") or 0)),
            "content_state": item.get("content_state") or "externalized",
            "artifact_ref": ref or "",
        }
        rendered = " ".join(
            f'{key}="{_xml_attr(value)}"' for key, value in attributes.items()
        )
        lines.append(f"  <attachment {rendered} />")
    lines.append("</user-attachments>")
    return {
        "role": "system",
        "content": "\n".join(lines),
        "metadata": {
            "kind": "user_attachments",
            "source": "runtime-generated",
            "instructions": False,
        },
    }


def latest_user_attachments(session: Any) -> list[dict[str, Any]]:
    for message in reversed(list(getattr(session, "messages", []) or [])):
        if not isinstance(message, Mapping) or str(message.get("role") or "") != "user":
            continue
        metadata = message.get("metadata")
        metadata = metadata if isinstance(metadata, Mapping) else {}
        if str(metadata.get("source") or "").startswith("runtime-generated"):
            continue
        raw = metadata.get("attachments")
        if isinstance(raw, list):
            return [dict(item) for item in raw if isinstance(item, Mapping)]
        ref = metadata.get("artifact_ref")
        if isinstance(ref, Mapping):
            return [{
                "name": ref.get("name") or "user-message",
                "media_type": ref.get("mime_type") or "text/plain",
                "size_bytes": ref.get("size_bytes") or metadata.get("original_content_bytes") or 0,
                "content_state": "externalized",
                "artifact_ref": dict(ref),
            }]
        return []
    return []


def _xml_attr(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
