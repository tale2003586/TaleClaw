from contextlib import contextmanager
from copy import deepcopy
import errno
import hashlib
import html
import json
import os
from pathlib import Path
import re
import stat
import threading
from typing import Any
import uuid

import yaml


SKILL_MAX_BYTES = 256 * 1024


class SkillConflictError(RuntimeError):
    """The document changed after the caller read its revision."""


class SkillValidationError(ValueError):
    """A document, operation, or path is not a valid managed skill."""


class SkillNotFoundError(LookupError):
    """The requested skill document no longer exists."""


class SkillLoader:
    def __init__(self, skills_dir: Path, *, auto_refresh: bool = True):
        self.skills_dir = Path(skills_dir).absolute()
        self.skills: dict[str, dict[str, Any]] = {}
        self.auto_refresh = bool(auto_refresh)
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._snapshot = ""
        self.refresh(force=True)

    def refresh(self, *, force: bool = False) -> bool:
        """Atomically publish valid, enabled skills; isolate malformed documents.

        Read bounded document bytes so edits retaining their mtime/size are also
        visible. No global or per-session prompt cache owns another skill copy.
        """
        with self._lock:
            records = {skill_id: self._read_record(skill_id) for skill_id in self._document_ids()}
            names: dict[str, list[str]] = {}
            for skill_id, record in records.items():
                if record["parsed"]:
                    names.setdefault(record["meta"]["name"], []).append(skill_id)
            for name, ids in names.items():
                if len(ids) > 1:
                    for skill_id in ids:
                        records[skill_id]["diagnostics"].append(
                            f"Duplicate skill name '{name}' in: {', '.join(ids)}"
                        )
            fingerprint = hashlib.sha256(json.dumps([
                (skill_id, record["revision"], record["diagnostics"])
                for skill_id, record in records.items()
            ], ensure_ascii=False).encode("utf-8")).hexdigest()
            if not force and fingerprint == self._snapshot:
                return False
            self._records = records
            self.skills = {
                record["meta"]["name"]: {
                    "meta": record["meta"], "body": record["body"],
                    "path": str(self.skills_dir / record["path"]),
                }
                for record in records.values()
                if not record["diagnostics"] and record["meta"]["enabled"]
            }
            self._snapshot = fingerprint
            return True

    def _maybe_refresh(self) -> None:
        if self.auto_refresh:
            self.refresh()

    @staticmethod
    def _id_parts(skill_id: str) -> tuple[str, ...]:
        if not isinstance(skill_id, str) or not skill_id or "\\" in skill_id or "\x00" in skill_id:
            raise SkillValidationError("Skill id must be a relative directory under the skills directory.")
        if skill_id == ".":
            return ()
        parts = tuple(skill_id.split("/"))
        if any(part in {"", ".", ".."} for part in parts):
            raise SkillValidationError("Skill id must not contain absolute or traversing path components.")
        return parts

    @contextmanager
    def _directory(self, skill_id: str):
        parts = self._id_parts(skill_id)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        fd = None
        try:
            fd = os.open(self.skills_dir, flags)
            for part in parts:
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except FileNotFoundError as exc:
            raise SkillNotFoundError(f"Skill '{skill_id}' was not found.") from exc
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
                raise SkillValidationError("Skill directories must be real directories inside the skills directory.") from exc
            raise
        finally:
            if fd is not None:
                os.close(fd)

    def _document_ids(self) -> list[str]:
        found = []
        try:
            with self._directory(".") as root_fd:
                def visit(fd, parts):
                    with os.scandir(fd) as scan:
                        entries = sorted(scan, key=lambda item: item.name)
                        for entry in entries:
                            if entry.name == "SKILL.md":
                                found.append("/".join(parts) or ".")
                            elif entry.is_dir(follow_symlinks=False):
                                try:
                                    child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                                except OSError:
                                    continue
                                try:
                                    visit(child, (*parts, entry.name))
                                finally:
                                    os.close(child)
                visit(root_fd, ())
        except SkillNotFoundError:
            return []
        return sorted(found)

    @staticmethod
    def _read_at(directory_fd: int) -> tuple[bytes, int]:
        try:
            fd = os.open("SKILL.md", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
        except FileNotFoundError as exc:
            raise SkillNotFoundError("Skill document was not found.") from exc
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise SkillValidationError("SKILL.md must not be a symbolic link.") from exc
            raise
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise SkillValidationError("SKILL.md must be a regular file.")
            if info.st_size > SKILL_MAX_BYTES:
                raise SkillValidationError("SKILL.md exceeds the 256 KiB limit.")
            chunks, size = [], 0
            while size <= SKILL_MAX_BYTES:
                chunk = os.read(fd, min(64 * 1024, SKILL_MAX_BYTES + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
            if size > SKILL_MAX_BYTES:
                raise SkillValidationError("SKILL.md exceeds the 256 KiB limit.")
            return b"".join(chunks), stat.S_IMODE(info.st_mode)
        finally:
            os.close(fd)

    def _read_record(self, skill_id: str) -> dict[str, Any]:
        fallback = self.skills_dir.name if skill_id == "." else skill_id.rsplit("/", 1)[-1]
        record = {
            "id": skill_id, "path": "SKILL.md" if skill_id == "." else skill_id + "/SKILL.md",
            "meta": self._normalize_meta({}, fallback_name=fallback), "body": "",
            "revision": "", "diagnostics": [], "parsed": False,
        }
        try:
            with self._directory(skill_id) as directory_fd:
                data, _ = self._read_at(directory_fd)
            record["revision"] = hashlib.sha256(data).hexdigest()
            text = data.decode("utf-8")
            meta, body = self._parse_frontmatter(text)
            record["meta"] = self._normalize_meta(meta, fallback_name=fallback)
            record["body"] = body.strip()
            record["parsed"] = True
        except (OSError, SkillNotFoundError, SkillValidationError, UnicodeError) as exc:
            record["diagnostics"].append(str(exc))
        return record

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, bool]:
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].rstrip("\r\n") != "---":
            return {}, text, False
        end = next((index for index in range(1, len(lines)) if lines[index].rstrip("\r\n") == "---"), None)
        if end is None:
            raise SkillValidationError("YAML frontmatter must have a closing --- delimiter.")
        try:
            meta = yaml.safe_load("".join(lines[1:end]))
        except RecursionError as exc:
            raise SkillValidationError("YAML frontmatter is too deeply nested.") from exc
        except yaml.YAMLError as exc:
            raise SkillValidationError(f"Invalid YAML frontmatter: {exc}") from exc
        if meta is None:
            meta = {}
        if not isinstance(meta, dict):
            raise SkillValidationError("YAML frontmatter must be a mapping.")
        return meta, "".join(lines[end + 1:]), True

    def _parse_frontmatter(self, text: str) -> tuple[dict[str, Any], str]:
        meta, body, _ = self._split_frontmatter(text)
        return meta, body

    def _normalize_meta(self, meta: Any, *, fallback_name: str) -> dict[str, Any]:
        raw = dict(meta or {}) if isinstance(meta, dict) else {}
        name = raw.get("name", fallback_name)
        if not isinstance(name, str) or not name.strip() or any(ord(char) < 32 for char in name):
            raise SkillValidationError("Skill name must be a nonempty string without control characters.")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise SkillValidationError("Skill enabled must be true or false.")
        normalized = dict(raw)
        normalized["name"] = name.strip()
        normalized["enabled"] = enabled
        normalized["description"] = _single_line(str(raw.get("description") or "No description").strip())
        for field in ("tags", "triggers", "applies_to", "requires_tools", "references"):
            normalized[field] = _as_list(raw.get(field))
        normalized["safety"] = _normalize_safety(raw.get("safety"))
        try:
            normalized["priority"] = int(raw.get("priority", 0) or 0)
        except (TypeError, ValueError, OverflowError):
            normalized["priority"] = 0
        return normalized

    def management_catalog(self) -> dict[str, Any]:
        with self._lock:
            self.refresh()
            entries = []
            for record in self._records.values():
                meta = record["meta"]
                entries.append({
                    "id": record["id"], "path": record["path"],
                    "name": meta["name"], "description": meta["description"],
                    "enabled": meta["enabled"], "valid": not record["diagnostics"],
                    "diagnostics": list(record["diagnostics"]), "revision": record["revision"],
                    **{field: deepcopy(meta[field]) for field in ("tags", "triggers", "applies_to", "requires_tools")},
                })
            return {"skills": entries, "revision": self._snapshot}

    def get_document(self, skill_id: str) -> dict[str, str]:
        with self._lock, self._directory(skill_id) as directory_fd:
            data, _ = self._read_at(directory_fd)
            return {"id": skill_id, "content": data.decode("utf-8", errors="replace"),
                    "revision": hashlib.sha256(data).hexdigest()}

    def update_document(self, skill_id: str, expected_revision: str, *, content=None, enabled=None) -> dict[str, str]:
        """Compare-and-save under a process lock; external editors do not share it.

        The last revision check detects external edits before atomic replacement;
        it cannot provide a cross-process transaction with arbitrary writers.
        """
        if (content is None) == (enabled is None):
            raise SkillValidationError("Provide exactly one of content or enabled.")
        if content is not None and not isinstance(content, str):
            raise SkillValidationError("Skill content must be a string.")
        if enabled is not None and not isinstance(enabled, bool):
            raise SkillValidationError("Skill enabled must be a boolean.")
        if not isinstance(expected_revision, str) or not expected_revision:
            raise SkillValidationError("An expected document revision is required.")
        with self._lock, self._directory(skill_id) as directory_fd:
            previous, mode = self._read_at(directory_fd)
            if hashlib.sha256(previous).hexdigest() != expected_revision:
                raise SkillConflictError("Skill changed since it was opened; reload before saving.")
            if enabled is not None:
                try:
                    text = previous.decode("utf-8")
                except UnicodeError as exc:
                    raise SkillValidationError("Repair invalid UTF-8 content before changing enabled state.") from exc
                meta, body, _ = self._split_frontmatter(text)
                meta["enabled"] = enabled
                content = "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False) + "---\n" + body
            try:
                data = content.encode("utf-8")
            except UnicodeError as exc:
                raise SkillValidationError("Skill content must be valid UTF-8.") from exc
            if len(data) > SKILL_MAX_BYTES:
                raise SkillValidationError("SKILL.md exceeds the 256 KiB limit.")
            fallback = self.skills_dir.name if skill_id == "." else skill_id.rsplit("/", 1)[-1]
            meta, _ = self._parse_frontmatter(content)
            normalized = self._normalize_meta(meta, fallback_name=fallback)
            self.refresh()
            if any(other_id != skill_id and record["parsed"] and record["meta"]["name"] == normalized["name"]
                   for other_id, record in self._records.items()):
                raise SkillValidationError(f"Duplicate skill name '{normalized['name']}'.")
            self._atomic_replace(directory_fd, data, mode, expected_revision)
            self.refresh(force=True)
            return {"id": skill_id, "content": content, "revision": hashlib.sha256(data).hexdigest()}

    def _atomic_replace(self, directory_fd: int, data: bytes, mode: int, expected_revision: str) -> None:
        temporary = ".SKILL.md." + uuid.uuid4().hex + ".tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=directory_fd)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
            current, _ = self._read_at(directory_fd)
            if hashlib.sha256(current).hexdigest() != expected_revision:
                raise SkillConflictError("Skill changed during saving; reload before saving.")
            os.replace(temporary, "SKILL.md", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
            os.fsync(directory_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory_fd)
            except FileNotFoundError:
                pass

    def get_descriptions(self) -> str:
        with self._lock:
            self._maybe_refresh()
            if not self.skills:
                return "(no skills available)"
            lines = []
            for name, skill in sorted(self.skills.items(), key=lambda item: (-int(item[1]["meta"].get("priority") or 0), item[0])):
                meta = skill["meta"]
                line = f"  - {name}: {meta.get('description', 'No description')}"
                for label, field in (("tags", "tags"), ("triggers", "triggers"), ("applies_to", "applies_to"), ("tools", "requires_tools")):
                    rendered = _format_list(meta.get(field))
                    if rendered:
                        line += f" {label}=[{rendered}]"
                safety = _format_safety(meta.get("safety"))
                if safety:
                    line += f" safety={safety}"
                lines.append(line)
            return "\n".join(lines)

    def get_catalog(self) -> list[dict[str, Any]]:
        with self._lock:
            self._maybe_refresh()
            return [{"name": name, "path": skill["path"], **deepcopy(skill["meta"])}
                    for name, skill in sorted(self.skills.items())]

    def search(self, query: str, *, mode: str = "", allowed_names: tuple[str, ...] | None = None, limit: int = 3) -> list[dict[str, Any]]:
        with self._lock:
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
                fields = ((100, name), (80, " ".join(meta.get("triggers") or ())),
                          (60, " ".join(meta.get("tags") or ())), (40, str(meta.get("description") or "")))
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
                    matches.append({"name": name, "description": meta.get("description", "No description"),
                                    "score": score + int(meta.get("priority") or 0),
                                    "requires_tools": list(meta.get("requires_tools") or ())})
            return sorted(matches, key=lambda item: (-item["score"], item["name"]))[:limit]

    def get_content(self, name: str, *, mode: str = "", allowed_names: tuple[str, ...] | None = None) -> str:
        """Return the current body only when enabled, valid, and in agent scope."""
        with self._lock:
            self._maybe_refresh()
            skill = self.skills.get(name)
            allowed = set(allowed_names or ())
            if not skill:
                available = [key for key, item in self.skills.items()
                             if (not allowed or key in allowed) and (not mode or not item["meta"]["applies_to"] or mode in item["meta"]["applies_to"])]
                return f"Error: Unknown or unavailable skill '{name}'. Available: {', '.join(available)}"
            if allowed and name not in allowed:
                return f"Error: Skill '{name}' is outside this AgentSpec skill scope."
            applies_to = set(skill["meta"].get("applies_to") or ())
            if applies_to and mode and mode not in applies_to:
                return f"Error: Skill '{name}' does not apply to {mode} mode."
            return f'<skill name="{html.escape(name, quote=True)}">\n{skill["body"]}\n</skill>'


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
    return value if isinstance(value, dict) else str(value).strip()


def _format_list(value: Any) -> str:
    return ", ".join(_as_list(value))


def _format_safety(value: Any) -> str:
    if isinstance(value, dict):
        parts = [f"{key}:{val}" for key, val in sorted(value.items(), key=lambda item: str(item[0])) if str(val).strip()]
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


SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
SKILL_LOADER = SkillLoader(SKILLS_DIR)
