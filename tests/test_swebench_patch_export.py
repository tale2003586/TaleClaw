from __future__ import annotations

import subprocess
from pathlib import Path

from evaluation.swebench_adapter import git_diff


def _git(root, *args, input=None):
    return subprocess.run(["git", *args], cwd=root, input=input, capture_output=True, check=True).stdout


def _initialize(root):
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "fixture@example.invalid")
    _git(root, "config", "user.name", "Fixture")
    for name in ["staged.txt", "unstaged.txt", "deleted.txt"]:
        (root / name).write_text("original\n")
    (root / ".gitignore").write_text("ignored-*\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")


def test_export_replays_all_final_changes_without_mutating_index(tmp_path):
    repo = tmp_path / "repo"
    _initialize(repo)
    (repo / "staged.txt").write_text("staged\n")
    _git(repo, "add", "staged.txt")
    (repo / "staged.txt").write_text("final after staging\n")
    (repo / "unstaged.txt").write_text("unstaged\n")
    (repo / "deleted.txt").unlink()
    (repo / "new.bin").write_bytes(b"\x00\xffbinary\x00")
    (repo / "new file.txt").write_text("new text\n")
    (repo / "ignored-staged.txt").write_text("intentional\n")
    _git(repo, "add", "-f", "ignored-staged.txt")
    (repo / "ignored-secret.txt").write_text("must not export\n")
    original_index = (repo / ".git" / "index").read_bytes()
    original_status = _git(repo, "status", "--porcelain=v1", "-z")
    patch = git_diff(repo)
    assert (repo / ".git" / "index").read_bytes() == original_index
    assert _git(repo, "status", "--porcelain=v1", "-z") == original_status
    assert "GIT binary patch" in patch
    assert "ignored-secret" not in patch
    replay = tmp_path / "replay"
    _git(tmp_path, "clone", "-q", str(repo), str(replay))
    _git(replay, "apply", "--binary", "-", input=patch.encode())
    for name in ["staged.txt", "unstaged.txt", "new.bin", "new file.txt", "ignored-staged.txt"]:
        assert (replay / name).read_bytes() == (repo / name).read_bytes()
    assert not (replay / "deleted.txt").exists()


def test_export_uses_requested_base_even_if_agent_committed(tmp_path):
    repo = tmp_path / "repo"
    _initialize(repo)
    baseline = _git(repo, "rev-parse", "HEAD").decode().strip()
    (repo / "staged.txt").write_text("committed by agent\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "agent change")
    assert git_diff(repo) == ""
    assert "+committed by agent" in git_diff(repo, base_revision=baseline)
