"""Optional project instruction loading for coding agents."""

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
    ) -> None:
        self.budgeter = budgeter
        self.instruction_root = Path(instruction_root or Path.cwd()).resolve()
        self.instruction_limit = max(1000, int(instruction_limit))

    def build_instruction_block(
        self,
        agent_spec,
    ) -> tuple[str, list[ContextSection], list[dict[str, Any]]]:
        mode = str(getattr(agent_spec, "tool_mode", "bot") or "bot")
        if mode != "coding":
            return "", [], []
        path = self.instruction_root / "AGENTS.md"
        text, raw_text, truncated = self._read_instruction_file(path)
        if not text:
            return "", [], []
        budgeted = self.budgeter.apply(
            "project_instructions",
            text,
            raw_text=raw_text,
        )
        source = _relative_or_name(path, self.instruction_root)
        section = ContextSection.from_text(
            "project_instructions",
            budgeted.rendered_text,
            raw_text=raw_text,
            budget_chars=budgeted.budget_chars,
            truncated=truncated or budgeted.truncated,
            metadata={"mode": mode, "sources": [source], **budgeted.metadata},
        )
        block = (
            f"<instructions section=\"project_instructions\" source=\"{source}\">\n"
            f"{budgeted.rendered_text}\n</instructions>"
        )
        reductions = [budgeted.reduction] if budgeted.reduction is not None else []
        return block, [section], reductions

    def fingerprint(self, agent_spec, *, runtime_guidance: str) -> str:
        mode = str(getattr(agent_spec, "tool_mode", "bot") or "bot")
        payload = {
            "agent_name": str(getattr(agent_spec, "name", "") or ""),
            "tool_mode": mode,
            "agent_instructions": str(getattr(agent_spec, "instructions", "") or ""),
            "runtime_guidance": runtime_guidance,
            "instruction_root": str(self.instruction_root),
            "instruction_limit": self.instruction_limit,
            "budgeter_enabled": self.budgeter.enabled,
            "budget_rules": {
                name: _budget_rule_signature(self.budgeter.rules.get(name))
                for name in ("project_instructions",)
            },
            "instruction_files": [
                _instruction_file_signature(section, path)
                for section, path in self.instruction_files(mode)
            ],
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
            return [("project_instructions", self.instruction_root / "AGENTS.md")]
        return []

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
