from pathlib import Path
import re
from typing import Any

import yaml


class SkillLoader:
    def __init__(self, skills_dir: Path, *, auto_refresh: bool = True):
        self.skills_dir = skills_dir
        self.skills = {}
        self.auto_refresh = bool(auto_refresh)
        self._snapshot = {}
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        """Reload skill metadata and bodies when files are added, changed, or removed."""
        snapshot = self._scan_snapshot()
        if not force and snapshot == self._snapshot:
            return False
        self.skills = self._load_all()
        self._snapshot = snapshot
        return True

    def _maybe_refresh(self) -> None:
        if self.auto_refresh:
            self.refresh()

    def _scan_snapshot(self) -> dict[str, tuple[int, int]]:
        if not self.skills_dir.exists():
            return {}
        snapshot = {}
        for path in sorted(self.skills_dir.rglob("SKILL.md")):
            try:
                stat = path.stat()
            except OSError:
                continue
            snapshot[str(path)] = (stat.st_mtime_ns, stat.st_size)
        return snapshot

    def _load_all(self) -> dict[str, dict[str, Any]]:
        skills = {}
        if not self.skills_dir.exists():
            return skills
        for f in sorted(self.skills_dir.rglob("SKILL.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            meta, body = self._parse_frontmatter(text)
            meta = self._normalize_meta(meta, fallback_name=f.parent.name)
            name = meta["name"]
            skills[name] = {"meta": meta, "body": body, "path": str(f)}
        return skills

    def _parse_frontmatter(self, text: str) -> tuple:
        """Parse YAML frontmatter between --- delimiters."""
        match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
        if not match:
            return {}, text
        try:
            meta = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            meta = {}
        return meta, match.group(2).strip()

    def _normalize_meta(self, meta: Any, *, fallback_name: str) -> dict[str, Any]:
        raw = dict(meta or {}) if isinstance(meta, dict) else {}
        normalized = dict(raw)
        normalized["name"] = str(raw.get("name") or fallback_name).strip() or fallback_name
        normalized["description"] = _single_line(
            str(raw.get("description") or "No description").strip()
        )
        normalized["tags"] = _as_list(raw.get("tags"))
        normalized["triggers"] = _as_list(raw.get("triggers"))
        normalized["applies_to"] = _as_list(raw.get("applies_to"))
        normalized["requires_tools"] = _as_list(raw.get("requires_tools"))
        normalized["references"] = _as_list(raw.get("references"))
        normalized["safety"] = _normalize_safety(raw.get("safety"))
        try:
            normalized["priority"] = int(raw.get("priority", 0) or 0)
        except (TypeError, ValueError):
            normalized["priority"] = 0
        return normalized

    def get_descriptions(self) -> str:
        """Layer 1: short descriptions for the system prompt."""
        self._maybe_refresh()
        if not self.skills:
            return "(no skills available)"
        lines = []
        for name, skill in sorted(
            self.skills.items(),
            key=lambda item: (-int(item[1]["meta"].get("priority") or 0), item[0]),
        ):
            meta = skill["meta"]
            desc = skill["meta"].get("description", "No description")
            tags = _format_list(meta.get("tags"))
            triggers = _format_list(meta.get("triggers"))
            applies_to = _format_list(meta.get("applies_to"))
            requires_tools = _format_list(meta.get("requires_tools"))
            safety = _format_safety(meta.get("safety"))
            line = f"  - {name}: {desc}"
            if tags:
                line += f" tags=[{tags}]"
            if triggers:
                line += f" triggers=[{triggers}]"
            if applies_to:
                line += f" applies_to=[{applies_to}]"
            if requires_tools:
                line += f" tools=[{requires_tools}]"
            if safety:
                line += f" safety={safety}"
            lines.append(line)
        return "\n".join(lines)

    def get_catalog(self) -> list[dict[str, Any]]:
        self._maybe_refresh()
        return [
            {
                "name": name,
                "path": skill["path"],
                **dict(skill["meta"]),
            }
            for name, skill in sorted(self.skills.items())
        ]

    def get_content(self, name: str) -> str:
        """Layer 2: full skill body returned in tool_result."""
        self._maybe_refresh()
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{skill['body']}\n</skill>"


def _as_list(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if isinstance(value, str):
        parts = value.split(",") if "," in value else [value]
        return [part.strip() for part in parts if part.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()]


def _normalize_safety(value: Any) -> str | dict[str, Any]:
    if value in (None, ""):
        return ""
    if isinstance(value, dict):
        return value
    return str(value).strip()


def _format_list(value: Any) -> str:
    return ", ".join(_as_list(value))


def _format_safety(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            f"{key}:{val}"
            for key, val in sorted(value.items())
            if str(val).strip()
        ]
        return "{" + ", ".join(parts) + "}" if parts else ""
    return str(value or "").strip()


def _single_line(value: str) -> str:
    return " ".join(str(value or "").split())


SKILLS_DIR = Path.cwd() / "skills"
SKILL_LOADER = SkillLoader(SKILLS_DIR)
