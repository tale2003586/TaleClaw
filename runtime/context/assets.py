"""Prompt instruction and skill catalog loading."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import threading
from typing import Any

from runtime.context.sections import ContextSection


DEFAULT_INSTRUCTION_LIMIT = 12000


class PromptAssetsService:
    _instruction_cache_lock = threading.Lock()
    _instruction_cache: dict[Path, tuple[int, int, str]] = {}

    def __init__(
        self,
        *,
        budgeter,
        instruction_root: str | Path | None = None,
        instruction_limit: int = DEFAULT_INSTRUCTION_LIMIT,
        skill_loader=None,
    ) -> None:
        self.budgeter = budgeter
        self.instruction_root = Path(instruction_root or Path.cwd()).resolve()
        self.instruction_limit = max(1000, int(instruction_limit))
        self.skill_loader = skill_loader

    def build_instruction_block(
        self,
        profile,
    ) -> tuple[str, list[ContextSection], list[dict[str, Any]]]:
        mode = str(getattr(profile, "tool_mode", "bot") or "bot")
        grouped: dict[str, list[dict[str, Any]]] = {
            "mode_instructions": [],
            "project_instructions": [],
        }
        for section_name, path in self.instruction_files(mode):
            text, raw_text, truncated = self._read_instruction_file(path)
            if not text:
                continue
            grouped[section_name].append({
                "source": _relative_or_name(path, self.instruction_root),
                "text": text,
                "raw_text": raw_text,
                "truncated": truncated,
            })

        blocks = []
        report_sections = []
        reductions = []
        for section_name, items in grouped.items():
            if not items:
                continue
            rendered_text = "\n\n".join(item["text"] for item in items)
            raw_text = "\n\n".join(item["raw_text"] for item in items)
            budgeted = self.budgeter.apply(
                section_name,
                rendered_text,
                raw_text=raw_text,
            )
            if budgeted.reduction is not None:
                reductions.append(budgeted.reduction)
            sources = [item["source"] for item in items]
            blocks.append(
                f"<instructions section=\"{section_name}\" sources=\"{','.join(sources)}\">\n"
                f"{budgeted.rendered_text}\n"
                "</instructions>"
            )
            report_sections.append(
                ContextSection.from_text(
                    section_name,
                    budgeted.rendered_text,
                    raw_text=raw_text,
                    budget_chars=(
                        budgeted.budget_chars
                        if self.budgeter.enabled
                        else self.instruction_limit * len(items)
                    ),
                    truncated=(
                        any(item["truncated"] for item in items)
                        or budgeted.truncated
                    ),
                    metadata={
                        "mode": mode,
                        "sources": sources,
                        **budgeted.metadata,
                        "files": [
                            {
                                "source": item["source"],
                                "raw_chars": len(item["raw_text"]),
                                "rendered_chars": len(item["text"]),
                                "truncated": bool(item["truncated"]),
                            }
                            for item in items
                        ],
                    },
                )
            )
        return "\n\n".join(blocks), report_sections, reductions

    def build_skill_catalog_block(self) -> str:
        descriptions = self._skill_catalog_signature()
        if (
            not descriptions
            or descriptions == "(no skills available)"
            or descriptions.startswith("error:")
        ):
            return ""
        return (
            "<skill-catalog>\n"
            "Use load_skill(name=\"...\") when the user request matches a skill's "
            "description, triggers, tags, or required workflow. Load only the relevant "
            "skill body before applying it.\n\n"
            "Available skills:\n"
            f"{descriptions.strip()}\n"
            "</skill-catalog>"
        )

    def fingerprint(self, profile, *, runtime_guidance: str) -> str:
        mode = str(getattr(profile, "tool_mode", "bot") or "bot")
        payload = {
            "profile_name": str(getattr(profile, "name", "") or ""),
            "tool_mode": mode,
            "profile_prompt": str(getattr(profile, "system_prompt", "") or ""),
            "runtime_guidance": runtime_guidance,
            "instruction_root": str(self.instruction_root),
            "instruction_limit": self.instruction_limit,
            "budgeter_enabled": self.budgeter.enabled,
            "budget_rules": {
                name: _budget_rule_signature(self.budgeter.rules.get(name))
                for name in (
                    "mode_instructions",
                    "project_instructions",
                    "skill_catalog",
                )
            },
            "instruction_files": [
                _instruction_file_signature(section, path)
                for section, path in self.instruction_files(mode)
            ],
            "skill_catalog": self._skill_catalog_signature(),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def instruction_files(self, mode: str) -> list[tuple[str, Path]]:
        if mode == "coding":
            return [
                ("mode_instructions", self.instruction_root / ".agent" / "coding.md"),
                ("project_instructions", self.instruction_root / "AGENTS.md"),
            ]
        return [
            ("mode_instructions", self.instruction_root / ".agent" / "assistant.md"),
        ]

    def _read_instruction_file(self, path: Path) -> tuple[str, str, bool]:
        if not path.is_file():
            return "", "", False
        path = path.resolve()
        try:
            stat = path.stat()
        except OSError:
            return "", "", False
        cache_key = (stat.st_mtime_ns, stat.st_size)
        with self._instruction_cache_lock:
            cached = self._instruction_cache.get(path)
        if cached is not None and cached[:2] == cache_key:
            text = cached[2]
        else:
            try:
                text = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                return "", "", False
            with self._instruction_cache_lock:
                self._instruction_cache[path] = (*cache_key, text)
        if len(text) <= self.instruction_limit:
            return text, text, False
        rendered = text[: self.instruction_limit].rstrip() + "\n\n...[truncated]"
        return rendered, text, True

    def _skill_catalog_signature(self) -> str:
        if self.skill_loader is None:
            return ""
        try:
            return str(self.skill_loader.get_descriptions() or "")
        except Exception as exc:
            return f"error:{type(exc).__name__}:{exc}"


def _relative_or_name(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.name


def _instruction_file_signature(section: str, path: Path) -> dict[str, Any]:
    try:
        resolved = path.resolve()
        stat = resolved.stat()
    except OSError:
        return {"section": section, "path": str(path), "exists": False}
    return {
        "section": section,
        "path": str(resolved),
        "exists": True,
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    }


def _budget_rule_signature(rule) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "budget_chars": getattr(rule, "budget_chars", None),
        "floor_chars": getattr(rule, "floor_chars", None),
        "strategy": getattr(rule, "strategy", None),
        "keep_head_turns": getattr(rule, "keep_head_turns", None),
        "keep_tail_turns": getattr(rule, "keep_tail_turns", None),
        "summary_chars": getattr(rule, "summary_chars", None),
        "keep_recent_results": getattr(rule, "keep_recent_results", None),
        "preserve_tools": list(getattr(rule, "preserve_tools", ()) or ()),
    }
