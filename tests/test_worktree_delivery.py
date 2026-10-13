"""Real Git integration invariants; no model, network or production database."""
from pathlib import Path

import pytest

from runtime.worktrees import WorktreeError, WorktreeService, git_run, resolve_commit
from runtime.worktree_delivery import (
    DeliveryConflictError, DeliveryNotFoundError, WorktreeDeliveryService,
)


@pytest.fixture
def delivery_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_run(repo, ["init", "-b", "main"])
    (repo / "value.txt").write_text("base\n")
    git_run(repo, ["add", "."])
    git_run(repo, ["-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                   "-c", "commit.gpgSign=false", "commit", "-m", "base"])
    worktrees = WorktreeService(tmp_path / "managed")
    service = WorktreeDeliveryService(worktrees)
    binding = worktrees.create(repo, task_id="task-a", owner_id="alice")
    return repo, worktrees, service, binding


def change_and_checkpoint(service, binding, content="agent A\n"):
    (Path(binding["workspace_root"]) / "value.txt").write_text(content)
    return service.checkpoint(binding["worktree_id"], owner_id="alice")


def test_checkpoint_preserves_task_and_never_stages_user_checkout(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    original = resolve_commit(repo)
    (repo / "value.txt").write_text("user dirty\n")
    (repo / "untracked.txt").write_text("user data\n")
    result = change_and_checkpoint(service, binding)
    assert resolve_commit(repo) == original
    assert (repo / "value.txt").read_text() == "user dirty\n"
    assert (repo / "untracked.txt").read_text() == "user data\n"
    assert git_run(repo, ["diff", "--cached", "--name-only"]).stdout == ""
    assert result["commit"] == resolve_commit(Path(binding["workspace_root"]))
    assert resolve_commit(repo, result["ref"]) == result["commit"]
    assert worktrees.remove(binding["worktree_id"], owner_id="alice")["status"] == "removed"
    assert resolve_commit(repo, result["ref"]) == result["commit"]


def test_candidate_is_fixed_if_task_later_changes_and_publish_leaves_target(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    target = resolve_commit(repo)
    c1 = change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    c2 = change_and_checkpoint(service, binding, "agent later\n")
    assert c1["commit"] != c2["commit"]
    assert candidate["source_sha"] == c1["commit"]
    verified = service.verify(candidate["id"], owner_id="alice", command="test \"$(cat value.txt)\" = 'agent A'")
    assert verified["status"] == "verified"
    assert verified["verification"]["candidate_sha"] == candidate["candidate_sha"]
    published = service.publish(candidate["id"], owner_id="alice")
    assert published["status"] == "published"
    assert resolve_commit(repo, published["published_ref"]) == candidate["candidate_sha"]
    assert resolve_commit(repo) == target
    assert (repo / "value.txt").read_text() == "base\n"
    assert service.publish(candidate["id"], owner_id="alice")["published_ref"] == published["published_ref"]


def test_target_move_invalidates_old_candidate(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    assert service.verify(candidate["id"], owner_id="alice", command="true")["status"] == "verified"
    (repo / "other.txt").write_text("target advanced\n")
    git_run(repo, ["add", "."])
    service._commit(repo, "advance target fixture")
    assert service.get(candidate["id"], owner_id="alice")["status"] == "stale"
    with pytest.raises(DeliveryConflictError):
        service.publish(candidate["id"], owner_id="alice")


def test_merge_conflict_keeps_target_and_task_intact(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    source = change_and_checkpoint(service, binding)
    (repo / "value.txt").write_text("target change\n")
    git_run(repo, ["add", "."])
    service._commit(repo, "conflicting target fixture")
    target = resolve_commit(repo)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    assert candidate["status"] == "conflict"
    assert candidate["conflicts"] == ["value.txt"]
    assert resolve_commit(repo) == target
    assert resolve_commit(Path(binding["workspace_root"])) == source["commit"]
    with pytest.raises(DeliveryConflictError):
        service.verify(candidate["id"], owner_id="alice", command="true")


def test_dirty_task_must_be_checkpointed_before_candidate(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    (Path(binding["workspace_root"]) / "new.txt").write_text("untracked")
    with pytest.raises(DeliveryConflictError):
        service.prepare(binding["worktree_id"], owner_id="alice")
    assert worktrees.inspect(binding["worktree_id"], owner_id="alice")["status"] == "ready"


@pytest.mark.parametrize("command", ["exit 3", "printf changed > value.txt"])
def test_failing_or_source_mutating_verifier_cannot_publish(delivery_repo, command):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    failed = service.verify(candidate["id"], owner_id="alice", command=command)
    assert failed["status"] == "failed"
    assert failed["verification"]["passed"] is False
    with pytest.raises(DeliveryConflictError):
        service.publish(candidate["id"], owner_id="alice")


def test_cross_owner_and_path_traversal_do_not_open_record_locks(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    with pytest.raises(DeliveryNotFoundError):
        service.get(candidate["id"], owner_id="bob")
    assert service.list(owner_id="bob") == []
    for identifier in ("../../escape", "", "dl_../repo"):
        with pytest.raises(ValueError):
            service.verify(identifier, owner_id="alice", command="true")
    assert not (worktrees.root / "escape.lock").exists()


def test_checked_out_user_branch_is_not_a_publish_destination(delivery_repo):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    with pytest.raises(ValueError):
        service.prepare(binding["worktree_id"], owner_id="alice", target_ref="refs/heads/main\ncreate refs/heads/evil HEAD")
    candidate = service.prepare(binding["worktree_id"], owner_id="alice", target_ref="refs/heads/main")
    with pytest.raises(DeliveryConflictError):
        service.publish(candidate["id"], owner_id="alice")
    assert git_run(repo, ["symbolic-ref", "HEAD"]).stdout.strip() == "refs/heads/main"


@pytest.mark.parametrize("command", ["sudo true", "cd /tmp && true", "rm -rf /tmp"])
def test_verification_uses_existing_shell_guards(delivery_repo, command):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    with pytest.raises(ValueError):
        service.verify(candidate["id"], owner_id="alice", command=command)
    assert service.get(candidate["id"], owner_id="alice")["status"] == "prepared"


def test_target_race_is_checked_inside_ref_transaction(delivery_repo, monkeypatch):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    service.verify(candidate["id"], owner_id="alice", command="true")
    original = service._update_refs
    def move_then_publish(source, transaction):
        git_run(repo, ["update-ref", "refs/heads/main", candidate["source_sha"]])
        return original(source, transaction)
    monkeypatch.setattr(service, "_update_refs", move_then_publish)
    with pytest.raises(DeliveryConflictError):
        service.publish(candidate["id"], owner_id="alice")
    assert git_run(repo, ["rev-parse", "--verify", f"refs/heads/taleclaw/deliveries/{candidate['id']}"], check=False).returncode != 0


@pytest.mark.parametrize("stage", ["preparing", "verifying"])
def test_exited_delivery_owner_is_shown_interrupted_without_replay(delivery_repo, stage):
    repo, worktrees, service, binding = delivery_repo
    change_and_checkpoint(service, binding)
    candidate = service.prepare(binding["worktree_id"], owner_id="alice")
    candidate.update(status=stage, operation_owner={"pid": 0, "started": "stale-identity"})
    service._save(candidate)
    shown = service.get(candidate["id"], owner_id="alice")
    assert shown["status"] == "interrupted"
    assert Path(candidate["workspace_root"]).is_dir()
    assert worktrees.inspect(candidate["worktree_id"], owner_id="alice")["status"] == "ready"
    with pytest.raises(DeliveryConflictError):
        service.publish(candidate["id"], owner_id="alice")
