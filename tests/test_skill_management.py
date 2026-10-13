from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
from pathlib import Path
import threading

import pytest

import skill_runtime
from skill_runtime import SkillLoader


def write_skill(root, directory="review", text="---\nname: review\ndescription: Review code\n---\nReview carefully.\n"):
    path = root / directory / "SKILL.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_enabled_frontmatter_blocks_search_and_direct_load(tmp_path):
    write_skill(tmp_path, text="---\nname: review\nenabled: false\n---\nsecret workflow\n")
    loader = SkillLoader(tmp_path)
    assert loader.search("review") == []
    assert "secret workflow" not in loader.get_content("review")
    assert loader.get_catalog() == []


def test_management_catalog_and_revision_are_separate_from_runtime(tmp_path):
    path = write_skill(tmp_path)
    loader = SkillLoader(tmp_path)
    catalog = loader.management_catalog()
    entry = catalog["skills"][0]
    assert {"id", "name", "description", "enabled", "valid", "diagnostics", "revision", "path", "tags", "triggers", "applies_to", "requires_tools"} <= entry.keys()
    assert entry["id"] == "review"
    assert entry["path"] == "review/SKILL.md"
    assert entry["valid"] is True and entry["enabled"] is True
    doc = loader.get_document("review")
    assert doc == {"id": "review", "content": path.read_text(), "revision": hashlib.sha256(path.read_bytes()).hexdigest()}
    entry["tags"].append("corrupt")
    assert loader.management_catalog()["skills"][0]["tags"] == []
    assert loader.management_catalog()["revision"] == catalog["revision"]


def test_toggle_is_atomic_persistent_and_preserves_metadata_and_body(tmp_path):
    path = write_skill(tmp_path, text="---\nname: review\ntags: [code, audit]\ncustom: {nested: value}\n---\n# Exact body\n  preserve spacing\n")
    path.chmod(0o640)
    loader = SkillLoader(tmp_path)
    before = loader.get_document("review")
    disabled = loader.update_document("review", before["revision"], enabled=False)
    assert disabled["revision"] != before["revision"]
    assert "# Exact body\n  preserve spacing\n" in disabled["content"]
    assert path.stat().st_mode & 0o777 == 0o640
    assert loader.get_catalog() == []
    assert SkillLoader(tmp_path).get_catalog() == []
    assert loader.management_catalog()["skills"][0]["enabled"] is False
    enabled = loader.update_document("review", disabled["revision"], enabled=True)
    assert loader.get_catalog()[0]["custom"] == {"nested": "value"}
    assert loader.get_catalog()[0]["tags"] == ["code", "audit"]
    assert "Exact body" in loader.get_content("review")
    assert enabled["revision"] == loader.get_document("review")["revision"]


def test_stale_and_concurrent_saves_do_not_overwrite_new_content(tmp_path):
    write_skill(tmp_path)
    loader = SkillLoader(tmp_path)
    original = loader.get_document("review")
    barrier = threading.Barrier(2)
    def save(index):
        barrier.wait(timeout=3)
        try:
            return loader.update_document("review", original["revision"], content=f"# Revision {index}\n")
        except skill_runtime.SkillConflictError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(save, range(2)))
    winner = next(item for item in outcomes if item is not None)
    assert sum(item is not None for item in outcomes) == 1
    assert loader.get_document("review") == winner


def test_same_stat_external_changes_are_detected_and_conflict(tmp_path):
    path = write_skill(tmp_path, text="# First workflow\n")
    loader = SkillLoader(tmp_path)
    original = loader.get_document("review")
    stat = path.stat()
    path.write_text("# Other workflow\n")
    assert path.stat().st_size == stat.st_size
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert "Other workflow" in loader.get_content("review")
    with pytest.raises(skill_runtime.SkillConflictError):
        loader.update_document("review", original["revision"], enabled=False)


def test_invalid_documents_are_isolated_and_repairable(tmp_path):
    write_skill(tmp_path)
    write_skill(tmp_path, "broken", "---\nname: [invalid\n---\nbody\n")
    loader = SkillLoader(tmp_path)
    catalog = {item["id"]: item for item in loader.management_catalog()["skills"]}
    assert catalog["review"]["valid"] is True
    assert catalog["broken"]["valid"] is False and catalog["broken"]["diagnostics"]
    assert [item["name"] for item in loader.get_catalog()] == ["review"]
    broken = loader.get_document("broken")
    fixed = loader.update_document("broken", broken["revision"], content="---\nname: repaired\n---\nRepaired body\n")
    assert fixed["id"] == "broken"
    assert "Repaired body" in loader.get_content("repaired")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("broken", fixed["revision"], content="---\nname: [invalid\n---\nbody\n")
    assert loader.get_document("broken") == fixed


def test_duplicate_names_are_all_quarantined_and_rename_conflicts_rejected(tmp_path):
    one = write_skill(tmp_path, "one")
    write_skill(tmp_path, "two")
    loader = SkillLoader(tmp_path)
    assert loader.get_catalog() == []
    assert all(not item["valid"] and item["diagnostics"] for item in loader.management_catalog()["skills"])
    doc = loader.get_document("two")
    loader.update_document("two", doc["revision"], content="---\nname: other\n---\nOther body\n")
    assert {item["name"] for item in loader.get_catalog()} == {"review", "other"}
    doc = loader.get_document("two")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("two", doc["revision"], content=one.read_text())


def test_plain_root_document_and_external_removal(tmp_path):
    path = write_skill(tmp_path, ".", "# Plain skill\n")
    loader = SkillLoader(tmp_path)
    assert loader.management_catalog()["skills"][0]["id"] == "."
    doc = loader.get_document(".")
    loader.update_document(".", doc["revision"], enabled=False)
    assert loader.get_catalog() == []
    path.unlink()
    assert loader.management_catalog()["skills"] == []
    with pytest.raises(skill_runtime.SkillNotFoundError):
        loader.get_document(".")


@pytest.mark.parametrize("kwargs", [{}, {"enabled": "false"}, {"enabled": 0}, {"enabled": True, "content": "both"}, {"content": 123}])
def test_invalid_update_arguments_do_not_write(tmp_path, kwargs):
    write_skill(tmp_path)
    loader = SkillLoader(tmp_path)
    before = loader.get_document("review")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("review", before["revision"], **kwargs)
    assert loader.get_document("review") == before


def test_boundaries_symlinks_fifo_and_size_are_rejected(tmp_path):
    root = tmp_path / "skills"
    write_skill(root)
    outside = tmp_path / "outside"
    write_skill(outside, ".", "External secret\n")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    (root / "file-link").mkdir()
    (root / "file-link" / "SKILL.md").symlink_to(outside / "SKILL.md")
    loader = SkillLoader(root)
    for skill_id in ("../outside", str(outside), "linked", "file-link", "review/../linked"):
        with pytest.raises(skill_runtime.SkillValidationError):
            loader.get_document(skill_id)
    assert "External secret" not in str(loader.management_catalog())
    pipe = root / "pipe"
    pipe.mkdir()
    os.mkfifo(pipe / "SKILL.md")
    big = write_skill(root, "big", "x" * (256 * 1024 + 1))
    catalog = {item["id"]: item for item in loader.management_catalog()["skills"]}
    assert catalog["pipe"]["valid"] is False
    assert catalog["big"]["valid"] is False
    current = loader.get_document("review")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("review", current["revision"], content="x" * (256 * 1024 + 1))
    assert loader.get_document("review") == current


def test_failed_atomic_replace_keeps_document_and_removes_temporary_file(tmp_path, monkeypatch):
    path = write_skill(tmp_path)
    loader = SkillLoader(tmp_path)
    before = loader.get_document("review")
    def fail(*args, **kwargs):
        raise OSError("replace failed")
    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        loader.update_document("review", before["revision"], content="# New body\n")
    assert path.read_text() == before["content"]
    assert sorted(item.name for item in path.parent.iterdir()) == ["SKILL.md"]


def test_global_directory_is_anchored_to_source_not_cwd():
    assert skill_runtime.SKILLS_DIR == Path(skill_runtime.__file__).resolve().parents[1] / "skills"


@pytest.mark.parametrize("frontmatter", ["[]", "false", "0", "a scalar"])
def test_non_mapping_frontmatter_is_invalid_even_when_falsey(tmp_path, frontmatter):
    write_skill(tmp_path)
    write_skill(tmp_path, "invalid", f"---\n{frontmatter}\n---\nBody\n")
    loader = SkillLoader(tmp_path)
    entry = next(item for item in loader.management_catalog()["skills"] if item["id"] == "invalid")
    assert entry["valid"] is False
    doc = loader.get_document("review")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("review", doc["revision"], content=f"---\n{frontmatter}\n---\nBody\n")


@pytest.mark.parametrize("priority", [".inf", "-.inf", ".nan"])
def test_unrepresentable_priority_does_not_break_other_skills(tmp_path, priority):
    write_skill(tmp_path)
    write_skill(tmp_path, "edge", f"---\nname: edge\npriority: {priority}\n---\nEdge body\n")
    loader = SkillLoader(tmp_path)
    assert "Review carefully" in loader.get_content("review")
    assert {item["name"] for item in loader.get_catalog()} == {"review", "edge"}
    assert "edge" in loader.get_descriptions()


def test_deep_yaml_is_isolated_and_rejected_on_save(tmp_path):
    write_skill(tmp_path)
    nested = "---\nname: deep\nvalue: " + "[" * 1500 + "0" + "]" * 1500 + "\n---\nBody\n"
    write_skill(tmp_path, "deep", nested)
    loader = SkillLoader(tmp_path)
    invalid = next(item for item in loader.management_catalog()["skills"] if item["id"] == "deep")
    assert invalid["valid"] is False
    assert "Review carefully" in loader.get_content("review")
    doc = loader.get_document("review")
    with pytest.raises(skill_runtime.SkillValidationError):
        loader.update_document("review", doc["revision"], content=nested)


def test_mixed_safety_keys_keep_descriptions_readable(tmp_path):
    write_skill(tmp_path, text="---\nname: review\nsafety:\n  1: low\n  risk: low\n---\nBody\n")
    loader = SkillLoader(tmp_path)
    assert "review:" in loader.get_descriptions()


def test_bad_utf8_and_non_boolean_enabled_are_isolated(tmp_path):
    write_skill(tmp_path)
    bad = write_skill(tmp_path, "encoding")
    bad.write_bytes(b"bad utf8 \xff")
    write_skill(tmp_path, "flag", "---\nname: flag\nenabled: 'false'\n---\nBody\n")
    loader = SkillLoader(tmp_path)
    entries = {item["id"]: item for item in loader.management_catalog()["skills"]}
    assert entries["encoding"]["valid"] is False
    assert entries["flag"]["valid"] is False
    assert [item["name"] for item in loader.get_catalog()] == ["review"]
    broken = loader.get_document("encoding")
    loader.update_document("encoding", broken["revision"], content="# Repaired UTF-8\n")
    assert "Repaired UTF-8" in loader.get_content("encoding")
