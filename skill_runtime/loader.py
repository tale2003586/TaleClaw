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

    def search(
        self,
        query: str,
        *,
        mode: str = "",
        allowed_names: tuple[str, ...] | None = None,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        self._maybe_refresh()
        normalized = _normalize_search_text(query)
        query_tokens = set(_search_tokens(normalized))
        allowed = set(allowed_names or ())
        matches = []
        for name, skill in self.skills.items():
            meta = skill["meta"]
            if allowed and name not in allowed:
                continue
            applies_to = set(meta.get("applies_to") or ())
            if applies_to and mode and mode not in applies_to:
                continue
            fields = (
                (100, name),
                (80, " ".join(meta.get("triggers") or ())),
                (60, " ".join(meta.get("tags") or ())),
                (40, str(meta.get("description") or "")),
            )
            score = 0
            for weight, value in fields:
                candidate = _normalize_search_text(value)
                if not candidate:
                    continue
                if normalized == candidate:
                    score = max(score, weight + 20)
                elif candidate in normalized or normalized in candidate:
                    score = max(score, weight)
                else:
                    overlap = query_tokens & set(_search_tokens(candidate))
                    if overlap:
                        score = max(score, min(weight, len(overlap) * max(8, weight // 3)))
            if score:
                matches.append({
                    "name": name,
                    "description": meta.get("description", "No description"),
                    "score": score + int(meta.get("priority") or 0),
                    "requires_tools": list(meta.get("requires_tools") or ()),
                })
        return sorted(matches, key=lambda item: (-item["score"], item["name"]))[:limit]

    def get_content(
        self,
        name: str,
        *,
        mode: str = "",
        allowed_names: tuple[str, ...] | None = None,
    ) -> str:
        """Layer 2: full skill body returned in tool_result."""
        self._maybe_refresh()
        skill = self.skills.get(name)
        if not skill:
            return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        allowed = set(allowed_names or ())
        if allowed and name not in allowed:
            return f"Error: Skill '{name}' is outside this AgentSpec skill scope."
        applies_to = set(skill["meta"].get("applies_to") or ())
        if applies_to and mode and mode not in applies_to:
            return f"Error: Skill '{name}' does not apply to {mode} mode."
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


def _normalize_search_text(value: str) -> str:
    return " ".join(str(value or "").lower().replace("_", " ").split())


def _search_tokens(value: str) -> list[str]:
    latin = re.findall(r"[a-z0-9]+", value)
    latin += [word[:-1] for word in latin if len(word) > 3 and word.endswith("s")]
    latin += [word[:-3] + "y" for word in latin if len(word) > 4 and word.endswith("ies")]
    cjk = re.findall(r"[\u4e00-\u9fff]{2,}", value)
    cjk_parts = [part for text in cjk for part in (text, *[text[i:i + 2] for i in range(len(text) - 1)])]
    return latin + cjk_parts


SKILLS_DIR = Path.cwd() / "skills"
SKILL_LOADER = SkillLoader(SKILLS_DIR)
