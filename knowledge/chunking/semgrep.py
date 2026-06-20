from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .base import KnowledgeChunk
from .utils import as_list, dedupe_strings, make_chunk, split_semantic_text


class SemgrepYamlChunking:
    source_type = "semgrep_rule"

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in {".yaml", ".yml"} and "semgrep-rules" in path.parts

    def chunk(
        self,
        path: Path,
        *,
        root: Path,
        chunk_chars: int = 1800,
        overlap_chars: int = 220,
    ) -> list[KnowledgeChunk]:
        try:
            import yaml
            text = path.read_text(encoding="utf-8")
            docs = list(yaml.safe_load_all(text))
        except Exception:
            return []
        rules = []
        for doc in docs:
            if isinstance(doc, dict) and isinstance(doc.get("rules"), list):
                rules.extend(rule for rule in doc["rules"] if isinstance(rule, dict))
        if not rules:
            return []
        chunks: list[KnowledgeChunk] = []
        for rule_index, rule in enumerate(rules):
            rule_id = str(rule.get("id") or f"{path.stem}:{rule_index + 1}")
            metadata = self._metadata(rule)
            header = self._header(rule_id=rule_id, metadata=metadata)
            rendered_rule = self._format_rule(rule)
            if len(rendered_rule) <= chunk_chars:
                parts = [(rendered_rule, 0, len(rendered_rule), 1)]
            else:
                prefix = self._format_rule(rule, include_patterns=False)
                parts = [(prefix, 0, len(prefix), 1)] if prefix.strip() else []
                patterns = self._format_patterns(rule)
                for part_index, (chunk_text, start, end) in enumerate(split_semantic_text(patterns, chunk_chars=chunk_chars, overlap_chars=0), start=2):
                    parts.append((f"Patterns part {part_index - 1}:\n{chunk_text}", start, end, part_index))
            rule_start = self._rule_start(text, rule_id)
            for body, start, end, part in parts:
                chunks.append(
                    make_chunk(
                        path=path,
                        root=root,
                        title=rule_id,
                        body=f"{header}\n\n{body}",
                        chunk_index=len(chunks),
                        char_start=max(0, rule_start + start),
                        char_end=max(0, rule_start + end),
                        source_type=self.source_type,
                        metadata={
                            **metadata,
                            "corpus_type": "semgrep_rule",
                            "rule_id": rule_id,
                            "part": part,
                            "filename": path.name,
                            "parent": path.parent.name,
                            "strategy_version": 2,
                        },
                    )
                )
        return chunks

    def _metadata(self, rule: dict[str, Any]) -> dict[str, Any]:
        raw = rule.get("metadata") if isinstance(rule.get("metadata"), dict) else {}
        return {
            "severity": str(rule.get("severity") or "").strip(),
            "message": str(rule.get("message") or "").strip(),
            "languages": [str(item) for item in as_list(rule.get("languages")) if str(item).strip()],
            "cwe": self._metadata_values(raw, ("cwe", "cwe_id", "cwe_ids", "owasp")),
            "category": str(raw.get("category") or "").strip(),
            "technology": self._metadata_values(raw, ("technology", "technologies")),
            "confidence": str(raw.get("confidence") or "").strip(),
            "likelihood": str(raw.get("likelihood") or "").strip(),
            "impact": str(raw.get("impact") or "").strip(),
        }

    def _metadata_values(self, metadata: dict[str, Any], keys: tuple[str, ...]) -> list[str]:
        values = []
        for key in keys:
            values.extend(str(item) for item in as_list(metadata.get(key)) if str(item).strip())
        return dedupe_strings(values)

    def _header(self, *, rule_id: str, metadata: dict[str, Any]) -> str:
        return "\n".join(
            [
                "Semgrep Rule",
                f"Rule ID: {rule_id}",
                f"Severity: {metadata.get('severity') or 'UNKNOWN'}",
                f"Languages: {', '.join(metadata.get('languages') or []) or 'UNKNOWN'}",
                f"CWE/OWASP: {', '.join(metadata.get('cwe') or []) or 'UNKNOWN'}",
                f"Category: {metadata.get('category') or 'UNKNOWN'}",
                f"Message: {metadata.get('message') or ''}",
            ]
        )

    def _format_rule(self, rule: dict[str, Any], *, include_patterns: bool = True) -> str:
        fields = {
            "message": rule.get("message"),
            "severity": rule.get("severity"),
            "languages": rule.get("languages"),
            "metadata": rule.get("metadata"),
        }
        if include_patterns:
            fields["patterns"] = self._pattern_payload(rule)
        return json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True)

    def _format_patterns(self, rule: dict[str, Any]) -> str:
        return json.dumps(self._pattern_payload(rule), ensure_ascii=False, indent=2, sort_keys=True)

    def _pattern_payload(self, rule: dict[str, Any]) -> dict[str, Any]:
        return {
            key: value
            for key, value in rule.items()
            if key.startswith("pattern") or key in {"mode", "options", "fix", "fix-regex"}
        }

    def _rule_start(self, text: str, rule_id: str) -> int:
        match = re.search(rf"(?m)^\s*-\s*id:\s*['\"]?{re.escape(rule_id)}['\"]?\s*$", text)
        if match:
            return match.start()
        match = re.search(re.escape(rule_id), text)
        return match.start() if match else 0
