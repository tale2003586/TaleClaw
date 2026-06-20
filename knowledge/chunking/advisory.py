from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import KnowledgeChunk
from .utils import as_list, dedupe_strings, make_chunk, split_semantic_text


class JsonAdvisoryChunking:
    source_type = "advisory"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() == ".json" and "advisory-database" in path.parts

    def chunk(
        self,
        path: Path,
        *,
        root: Path,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
    ) -> list[KnowledgeChunk]:
        try:
            text = path.read_text(encoding="utf-8")
            data = json.loads(text)
        except Exception:
            return []
        if not isinstance(data, dict) or not self._looks_like_advisory(data):
            return []
        advisory_id = str(data.get("id") or path.stem)
        aliases = [str(item) for item in as_list(data.get("aliases")) if str(item).strip()]
        severity = self._format_severity(data.get("severity"))
        packages = self._affected_packages(data)
        cwes = self._cwe_list(data)
        header = self._format_header(data, advisory_id=advisory_id, aliases=aliases, severity=severity, packages=packages, cwes=cwes)
        base_metadata = {
            "corpus_type": "advisory",
            "advisory_id": advisory_id,
            "aliases": aliases,
            "severity": severity,
            "packages": packages,
            "cwes": cwes,
            "filename": path.name,
            "parent": path.parent.name,
            "strategy_version": 2,
        }
        pieces: list[tuple[str, str, int, int, dict[str, Any]]] = []
        summary = str(data.get("summary") or "").strip()
        if summary:
            pieces.append(("summary", f"{header}\n\nSummary:\n{summary}", self._field_start(text, "summary"), self._field_end(text, "summary"), {}))
        details = str(data.get("details") or data.get("description") or "").strip()
        if details:
            detail_start = self._field_start(text, "details") or self._field_start(text, "description")
            for index, (chunk_text, start, end) in enumerate(split_semantic_text(details, chunk_chars=chunk_chars, overlap_chars=0), start=1):
                pieces.append(("details", f"{header}\n\nDetails part {index}:\n{chunk_text}", detail_start + start, detail_start + end, {"part": index}))
        affected = self._format_json_block(data.get("affected"))
        if affected:
            pieces.append(("affected", f"{header}\n\nAffected packages and ranges:\n{affected}", self._field_start(text, "affected"), self._field_end(text, "affected"), {}))
        references = self._format_references(data.get("references"))
        if references:
            pieces.append(("references", f"{header}\n\nReferences:\n{references}", self._field_start(text, "references"), self._field_end(text, "references"), {}))
        if not pieces:
            compact = json.dumps(data, ensure_ascii=False, indent=2)
            for index, (chunk_text, start, end) in enumerate(split_semantic_text(compact, chunk_chars=chunk_chars, overlap_chars=0), start=1):
                pieces.append(("raw", f"{header}\n\nRaw advisory part {index}:\n{chunk_text}", start, end, {"part": index}))
        title = self._title(data, advisory_id)
        return [
            make_chunk(
                path=path,
                root=root,
                title=title,
                body=body,
                chunk_index=index,
                char_start=max(0, start),
                char_end=max(0, end),
                source_type=self.source_type,
                metadata={**base_metadata, "field": field_name, **extra},
            )
            for index, (field_name, body, start, end, extra) in enumerate(pieces)
        ]

    def _looks_like_advisory(self, data: dict[str, Any]) -> bool:
        advisory_id = str(data.get("id") or "")
        aliases = as_list(data.get("aliases"))
        return bool(advisory_id.startswith(("GHSA-", "CVE-")) or any(str(alias).startswith("CVE-") for alias in aliases))

    def _format_header(self, data: dict[str, Any], *, advisory_id: str, aliases: list[str], severity: str, packages: list[str], cwes: list[str]) -> str:
        return "\n".join(
            [
                "CVE/GHSA Advisory",
                f"ID: {advisory_id}",
                f"Aliases: {', '.join(aliases) if aliases else 'None'}",
                f"Severity: {severity or 'UNKNOWN'}",
                f"Published: {data.get('published') or 'UNKNOWN'}",
                f"Modified: {data.get('modified') or 'UNKNOWN'}",
                f"Package: {', '.join(packages) if packages else 'UNKNOWN'}",
                f"CWEs: {', '.join(cwes) if cwes else 'UNKNOWN'}",
            ]
        )

    def _format_severity(self, value: Any) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            rendered = []
            for item in value:
                if isinstance(item, dict):
                    score = item.get("score")
                    severity_type = item.get("type")
                    rendered.append(f"{severity_type}:{score}" if score and severity_type else str(score or ""))
                elif item:
                    rendered.append(str(item))
            return ", ".join(item for item in rendered if item)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        return ""

    def _affected_packages(self, data: dict[str, Any]) -> list[str]:
        packages = []
        for item in as_list(data.get("affected")):
            package = item.get("package") if isinstance(item, dict) else None
            if not isinstance(package, dict):
                continue
            ecosystem = str(package.get("ecosystem") or "").strip()
            name = str(package.get("name") or "").strip()
            packages.append(f"{ecosystem}:{name}" if ecosystem and name else name)
        return dedupe_strings(packages)

    def _cwe_list(self, data: dict[str, Any]) -> list[str]:
        cwes = []
        database_specific = data.get("database_specific")
        if isinstance(database_specific, dict):
            cwes.extend(str(item) for item in as_list(database_specific.get("cwe_ids")))
            cwes.extend(str(item) for item in as_list(database_specific.get("cwes")))
        cwes.extend(str(item) for item in as_list(data.get("cwes")))
        return dedupe_strings(item for item in cwes if item and item != "None")

    def _format_json_block(self, value: Any) -> str:
        items = as_list(value)
        return json.dumps(items, ensure_ascii=False, indent=2) if items else ""

    def _format_references(self, value: Any) -> str:
        refs = []
        for item in as_list(value):
            if isinstance(item, dict):
                ref_type = item.get("type")
                url = item.get("url")
                refs.append(f"- {ref_type}: {url}" if ref_type and url else f"- {url}" if url else "")
            elif item:
                refs.append(f"- {item}")
        return "\n".join(ref for ref in refs if ref)

    def _field_start(self, text: str, field_name: str) -> int:
        match = re.search(rf'"{re.escape(field_name)}"\s*:', text)
        return match.start() if match else 0

    def _field_end(self, text: str, field_name: str) -> int:
        start = self._field_start(text, field_name)
        next_match = re.search(r'\n\s*"[^"]+"\s*:', text[start + 1 :])
        return start + 1 + next_match.start() if next_match else len(text)

    def _title(self, data: dict[str, Any], advisory_id: str) -> str:
        summary = str(data.get("summary") or "").strip()
        return f"{advisory_id}: {summary[:120]}" if summary else advisory_id
