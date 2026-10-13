from pathlib import Path

import pytest

from applications.coding.task_lifecycle import coding_task_options
from runtime.sessions import Session
from runtime.workspace import WorkspaceResolver
from runtime.worktrees import WorktreeService
from tests.test_long_request_externalization import _direct_coding_runner
from tests.test_task_continuation_regressions import SPEC, active
from tests.test_managed_worktrees import managed, commit


def test_coding_new_continue_branch_bind_distinct_source_versions(managed, tmp_path):
    source, service = managed
    runner, sessions = _direct_coding_runner(tmp_path / "runtime")
    runner.workspace_resolver = WorkspaceResolver(allowed_roots=[source], default_workspace=source)
    runner.worktree_service = service
    parent = Session("web:worktree", active_agent="coding", metadata={"user_id": "alice"})
    (source / "code.txt").write_text("USER_DIRTY")
    runner.run_coding_task(parent_session=parent, user_text="Implement", agent_spec=SPEC,
                           task_mode="new", workspace_mode="worktree")
    original = active(sessions, parent)
    binding = original.metadata["worktree_binding"]
    root = Path(binding["workspace_root"])
    assert root != source and (root / "code.txt").read_text() == "committed\n"
    assert original.metadata["repository"] == binding["repo_id"]
    assert str(runner.factory.root) not in str(root)
    assert parent.metadata["workspace_root"] == str(source)
    (root / "code.txt").write_text("TASK_COMMIT")
    sha = commit(root)
    runner.run_coding_task(parent_session=parent, user_text="Continue", agent_spec=SPEC)
    assert active(sessions, parent) is original
    assert original.metadata["worktree_binding"]["worktree_id"] == binding["worktree_id"]
    runner.run_coding_task(parent_session=parent, user_text="Alternative", agent_spec=SPEC, task_mode="branch")
    branched = active(sessions, parent)
    other = branched.metadata["worktree_binding"]
    assert other["base_commit"] == sha
    assert other["repo_id"] == binding["repo_id"]
    assert other["workspace_root"] != str(root)
    assert (Path(other["workspace_root"]) / "code.txt").read_text() == "TASK_COMMIT"
    assert (source / "code.txt").read_text() == "USER_DIRTY"


def test_worktree_selector_validation():
    assert coding_task_options({"mode": "new", "workspace_mode": "worktree", "base_ref": "HEAD"})["base_ref"] == "HEAD"
    for options in ({"workspace_mode": []}, {"workspace_mode": "other"}, {"base_ref": "-bad"},
                    {"mode": "continue", "base_ref": "HEAD"}, {"workspace_mode": "in_place", "base_ref": "HEAD"}):
        with pytest.raises(ValueError):
            coding_task_options(options)


def test_in_place_runtime_identity_is_published_before_execution(managed, tmp_path, monkeypatch):
    from types import SimpleNamespace
    from copy import deepcopy
    source, service = managed
    runner, _ = _direct_coding_runner(tmp_path / "runtime")
    runner.workspace_resolver = WorkspaceResolver(allowed_roots=[source], default_workspace=source)
    state = SimpleNamespace(metadata={})
    writes = []
    trace = SimpleNamespace(write_run_state=lambda run: writes.append(deepcopy(run.metadata)))
    parent = Session("web:legacy", active_agent="coding", metadata={"user_id": "alice"})
    def execute(**kwargs):
        assert writes[0]["workspace_root"] == str(source)
        assert writes[0]["task_id"] == kwargs["allocated_task_id"]
        assert "worktree_binding" not in writes[0]
        return "done"
    monkeypatch.setattr(runner, "_run_coding_task", execute)
    assert runner.run_coding_task(parent_session=parent, user_text="legacy", agent_spec=SPEC,
                                  run_state=state, trace_store=trace, task_mode="new") == "done"


def test_branch_to_explicit_in_place_does_not_inherit_managed_binding(managed, tmp_path):
    source, service = managed
    runner, sessions = _direct_coding_runner(tmp_path / "runtime")
    runner.workspace_resolver = WorkspaceResolver(allowed_roots=[source], default_workspace=source)
    runner.worktree_service = service
    parent = Session("web:mode-branch", active_agent="coding", metadata={"user_id": "alice"})
    runner.run_coding_task(parent_session=parent, user_text="Managed", agent_spec=SPEC,
                           task_mode="new", workspace_mode="worktree")
    runner.run_coding_task(parent_session=parent, user_text="Legacy branch", agent_spec=SPEC,
                           task_mode="branch", workspace_mode="in_place")
    branch = active(sessions, parent)
    assert branch.metadata["workspace_root"] == str(source)
    assert branch.metadata["workspace_mode"] == "in_place"
    assert "worktree_binding" not in branch.metadata
    assert "repo_id" not in branch.metadata


def test_web_default_applies_only_to_new_tasks_and_preserves_legacy_continuation(managed, tmp_path):
    source, service = managed
    runner, sessions = _direct_coding_runner(tmp_path / "runtime")
    runner.workspace_resolver = WorkspaceResolver(allowed_roots=[source], default_workspace=source)
    runner.worktree_service = service
    parent = Session("web:host-default", active_agent="coding", metadata={"user_id": "alice"})
    runner.run_coding_task(parent_session=parent, user_text="Legacy", agent_spec=SPEC, task_mode="new")
    original = active(sessions, parent)
    assert original.metadata["workspace_mode"] == "in_place"
    parent.metadata["coding_default_workspace_mode"] = "worktree"
    runner.run_coding_task(parent_session=parent, user_text="Continue legacy", agent_spec=SPEC)
    assert active(sessions, parent) is original
    assert original.metadata["workspace_mode"] == "in_place"
    runner.run_coding_task(parent_session=parent, user_text="Branch legacy", agent_spec=SPEC, task_mode="branch")
    assert active(sessions, parent).metadata["workspace_mode"] == "in_place"
    runner.run_coding_task(parent_session=parent, user_text="New web task", agent_spec=SPEC, task_mode="new")
    assert active(sessions, parent).metadata["workspace_mode"] == "worktree"
