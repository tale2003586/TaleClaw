"""Real temporary Git repositories; no production checkout or external service."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time

import pytest

from runtime.worktrees import WorktreeService, WorktreeError, WorktreeQuarantinedError, git_run, resolve_commit
from runtime.workspace_lease import WorkspaceBusyError, workspace_writer


def commit(path, message="change"):
    git_run(path, ["add", "--all"])
    git_run(path, ["-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", message])
    return resolve_commit(path)


@pytest.fixture
def managed(tmp_path, monkeypatch):
    monkeypatch.setenv("TALECLAW_RESOURCE_LOCK_ROOT", str(tmp_path / "resource-leases"))
    source = tmp_path / "source"
    source.mkdir()
    git_run(source, ["init", "-b", "main"])
    (source / "code.txt").write_text("committed\n")
    commit(source, "initial")
    service = WorktreeService(tmp_path / "managed")
    return source, service


def create(service, source, task="a", owner="alice", **kw):
    return service.create(source, task_id=task, owner_id=owner, **kw)


def test_new_worktree_excludes_dirty_source_and_preserves_source_index(managed):
    source, service = managed
    (source / "code.txt").write_text("staged\n")
    git_run(source, ["add", "code.txt"])
    (source / "code.txt").write_text("unstaged\n")
    (source / "untracked.txt").write_text("private")
    before = git_run(source, ["diff", "--cached"]).stdout
    binding = create(service, source)
    root = Path(binding["workspace_root"])
    assert binding["dirty_source_excluded"]
    assert (root / "code.txt").read_text() == "committed\n"
    assert not (root / "untracked.txt").exists()
    assert (source / "code.txt").read_text() == "unstaged\n"
    assert git_run(source, ["diff", "--cached"]).stdout == before
    assert (root / ".git").is_file()
    assert not root.is_relative_to(source)


def test_independent_worktrees_edit_and_stage_in_parallel(managed):
    source, service = managed
    a, b = create(service, source, "a"), create(service, source, "b")
    barrier = threading.Barrier(2)
    def edit(binding, text):
        with service.lease(binding):
            barrier.wait(timeout=5)
            path = Path(binding["workspace_root"])
            (path / "code.txt").write_text(text)
            git_run(path, ["add", "code.txt"])
            return git_run(path, ["diff", "--cached"]).stdout
    with ThreadPoolExecutor(2) as pool:
        fa, fb = pool.submit(edit, a, "ONLY_A\n"), pool.submit(edit, b, "ONLY_B\n")
        da, db = fa.result(), fb.result()
    assert "ONLY_A" in da and "ONLY_B" not in da
    assert "ONLY_B" in db and "ONLY_A" not in db
    assert not git_run(source, ["status", "--porcelain"]).stdout
    assert a["repo_id"] == b["repo_id"]


def test_managed_writer_excludes_same_workspace_and_in_place_writer(managed):
    source, service = managed
    binding = create(service, source)
    with service.lease(binding):
        with pytest.raises(WorkspaceBusyError):
            with service.lease(binding):
                pass
        with pytest.raises(WorkspaceBusyError):
            with workspace_writer(source):
                pass
    with workspace_writer(source):
        with pytest.raises(WorkspaceBusyError):
            with service.lease(binding):
                pass


def test_dead_writer_marker_blocks_reentry(managed):
    source, service = managed
    binding = create(service, source)
    code = ("from runtime.worktrees import WorktreeService; import json,os; "
            f"s=WorktreeService({str(service.root)!r}); b=json.loads({json.dumps(binding)!r}); "
            "lease=s.lease(b);lease.__enter__();os._exit(7)")
    result = subprocess.run([sys.executable, "-c", code], env=os.environ.copy())
    assert result.returncode == 7
    assert service.inspect(binding["worktree_id"])["status"] == "active"
    with pytest.raises(WorktreeQuarantinedError):
        with service.lease(binding):
            pass


def test_owner_alias_identity_and_overlap_fail_closed(managed, tmp_path):
    source, service = managed
    with pytest.raises(WorktreeError, match="overlap"):
        WorktreeService(source / "nested").create(source, task_id="x", owner_id="alice")
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    with pytest.raises(WorktreeError, match="symbolic"):
        create(service, alias)
    binding = create(service, source)
    with pytest.raises(WorktreeError):
        service.inspect(binding["worktree_id"], owner_id="bob")
    git_run(Path(binding["workspace_root"]), ["checkout", "--detach"])
    with pytest.raises(WorktreeError, match="identity"):
        service.inspect(binding["worktree_id"])


def test_git_environment_does_not_redirect_index_and_filters_do_not_execute(managed, monkeypatch, tmp_path):
    source, service = managed
    redirected = tmp_path / "forbidden-index"
    monkeypatch.setenv("GIT_INDEX_FILE", str(redirected))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "wrong"))
    binding = create(service, source)
    assert not redirected.exists()
    assert service.inspect(binding["worktree_id"])["base_commit"] == resolve_commit(source)
    git_run(source, ["config", "filter.unsafe.smudge", "touch SHOULD_NOT_RUN"])
    with pytest.raises(WorktreeError, match="filters"):
        create(service, source, "other")
    assert not (source / "SHOULD_NOT_RUN").exists()


def test_remove_requires_clean_inactive_and_independent_preservation(managed):
    source, service = managed
    binding = create(service, source)
    identifier = binding["worktree_id"]
    path = Path(binding["workspace_root"])
    (path / "untracked").write_text("keep")
    with pytest.raises(WorktreeError, match="dirty"):
        service.remove(identifier, owner_id="alice")
    head = commit(path)
    with pytest.raises(WorktreeError, match="Unpreserved"):
        service.remove(identifier, owner_id="alice", preserved_commit=head)
    reference = "refs/taleclaw/deliveries/test"
    git_run(path, ["update-ref", reference, head])
    service.preserve(identifier, owner_id="alice", ref=reference, commit=head)
    assert service.remove(identifier, owner_id="alice")["status"] == "removed"
    assert resolve_commit(source, reference) == head
    assert not path.exists()


def test_managed_commands_have_private_env_and_dependencies(managed, monkeypatch):
    from tools.command import run_command
    source, service = managed
    monkeypatch.setenv("DATABASE_URL", "postgres://must-not-inherit")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-inherit")
    monkeypatch.setenv("PGPASSWORD", "must-not-inherit")
    binding = create(service, source)
    code = "import os,sys,json; print(json.dumps({'env':dict(os.environ),'prefix':sys.prefix}))"
    result = run_command("python -c " + shlex.quote(code), cwd=binding["workspace_root"], managed_binding=binding)
    assert result.status == "success", result
    payload = json.loads(result)
    assert not {"DATABASE_URL", "OPENAI_API_KEY", "PGPASSWORD", "GIT_INDEX_FILE"} & payload["env"].keys()
    assert str(service.root / "runtime" / binding["worktree_id"]) in payload["prefix"]
    assert payload["env"]["TMPDIR"].startswith(str(service.root))
    assert payload["env"]["COMPOSE_PROJECT_NAME"].endswith(binding["worktree_id"])
    evidence = result.metadata["managed_environment"]
    assert evidence["virtual_env"] == payload["prefix"]
    assert evidence["python_version"] == sys.version.split()[0]
    assert evidence["tmpdir"] == payload["env"]["TMPDIR"]
    assert evidence["port"] == payload["env"]["PORT"]
    assert "must-not-inherit" not in json.dumps(result.metadata)


def test_detached_managed_child_is_stopped_before_resource_release(managed):
    from tools.command import run_command
    source, service = managed
    binding = create(service, source)
    late = Path(binding["workspace_root"]) / "late"
    child = "import os,time,pathlib; os.setsid(); time.sleep(1); pathlib.Path('late').write_text('unsafe')"
    launcher = "import subprocess; subprocess.Popen(['python','-c'," + repr(child) + "],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
    result = run_command("python -c " + shlex.quote(launcher), cwd=binding["workspace_root"], managed_binding=binding)
    assert result.status == "error"
    assert result.metadata["managed_descendants_exited"]
    time.sleep(1.1)
    assert not late.exists()
    assert service.inspect(binding["worktree_id"])["status"] == "ready"


@pytest.mark.parametrize("execution_facts", [
    {"process_group_exited": False},
    {"process_group_exited": True, "output_complete": False},
])
def test_unconfirmed_command_quarantines_workspace_and_resources(managed, monkeypatch, execution_facts):
    import tools.command as commands
    from tools.executor import ToolHandlerOutput
    source, service = managed
    binding = create(service, source)
    monkeypatch.setattr(commands, "_run_command", lambda *args, **kw: ToolHandlerOutput(
        "unknown", status="unknown", metadata=execution_facts))
    commands.run_command("anything", cwd=binding["workspace_root"], managed_binding=binding)
    assert service.inspect(binding["worktree_id"])["status"] == "quarantined"
    other = create(service, source, "other")
    with pytest.raises(WorktreeQuarantinedError, match="Previous command"):
        commands.run_command("anything", cwd=other["workspace_root"], managed_binding=other)


def test_busy_command_resource_returns_without_starting_or_waiting(managed, monkeypatch):
    import tools.command as commands
    source, service = managed
    first, second = create(service, source), create(service, source, "second")
    called = []
    monkeypatch.setattr(commands, "_run_command", lambda *args, **kw: called.append(True))
    with service.command_scope(first) as (_, completion):
        started = time.monotonic()
        result = commands.run_command("never run", cwd=second["workspace_root"], managed_binding=second,
                                      timeout_seconds=0.1)
        assert time.monotonic() - started < 1
        assert result.error_type == "ResourceBusy"
        assert result.effect_state == "not_started"
        assert not called
        completion["confirmed"] = True
    assert service.inspect(second["worktree_id"])["status"] == "ready"


@pytest.mark.parametrize("same_user", [True, False])
def test_unreadable_process_environment_is_not_same_user_exit_proof(tmp_path, monkeypatch, same_user):
    import tools.command as commands
    from types import SimpleNamespace
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "stat").write_text("123 (worker) S 1 2 3")
    original_stat, original_read = Path.stat, Path.read_bytes
    monkeypatch.setattr(commands, "Path", lambda path: proc if path == "/proc" else Path(path))
    def stat(path, **kwargs):
        if path == process:
            return SimpleNamespace(st_uid=os.getuid() if same_user else os.getuid() + 1)
        return original_stat(path, **kwargs)
    def read(path):
        if path == process / "environ":
            raise PermissionError("synthetic same-user process inspection denial")
        return original_read(path)
    monkeypatch.setattr(Path, "stat", stat)
    monkeypatch.setattr(Path, "read_bytes", read)
    if same_user:
        with pytest.raises(RuntimeError, match="Cannot confirm"):
            commands._tagged_children("tag")
    else:
        assert commands._tagged_children("tag") == []


def test_process_inspection_failure_quarantines_instead_of_releasing(managed, monkeypatch):
    import tools.command as commands
    source, service = managed
    binding = create(service, source)
    def unavailable(tag, baseline):
        raise RuntimeError("synthetic process visibility loss")
    monkeypatch.setattr(commands, "_tagged_children", unavailable)
    result = commands.run_command("true", cwd=binding["workspace_root"], managed_binding=binding)
    assert result.status == "unknown"
    assert result.error_type == "CommandExitUnconfirmed"
    assert service.inspect(binding["worktree_id"])["status"] == "quarantined"
    assert (Path(os.environ["TALECLAW_RESOURCE_LOCK_ROOT"]) / "active.json").is_file()


def test_replaced_common_repository_directory_invalidates_binding(managed):
    import shutil
    source, service = managed
    binding = create(service, source)
    original = source / ".git-original"
    (source / ".git").rename(original)
    shutil.copytree(original, source / ".git")
    with pytest.raises(WorktreeError, match="identity changed"):
        service.inspect(binding["worktree_id"])


def test_external_merge_driver_is_rejected_before_execution(managed):
    source, service = managed
    git_run(source, ["config", "merge.unsafe.driver", "touch SHOULD_NOT_RUN"])
    with pytest.raises(WorktreeError, match="merge drivers"):
        create(service, source)
    assert not (source / "SHOULD_NOT_RUN").exists()


def test_preexisting_unreadable_peer_is_excluded_by_pid_and_start_time(tmp_path, monkeypatch):
    import tools.command as commands
    proc = tmp_path / "proc"
    process = proc / "123"
    process.mkdir(parents=True)
    (process / "stat").write_text("123 (worker) S " + "0 " * 18 + "12345")
    monkeypatch.setattr(commands, "Path", lambda path: proc if path == "/proc" else Path(path))
    baseline = commands._process_baseline()
    def denied(path):
        raise PermissionError("synthetic process environment denial")
    monkeypatch.setattr(Path, "read_bytes", denied)
    assert commands._tagged_children("tag", baseline) == []
    (process / "stat").write_text("123 (worker) S " + "0 " * 18 + "67890")
    with pytest.raises(RuntimeError, match="Cannot confirm"):
        commands._tagged_children("tag", baseline)


def test_missing_and_other_owner_bindings_share_not_found_contract(managed):
    from runtime.worktrees import WorktreeNotFoundError
    source, service = managed
    binding = create(service, source)
    for identifier, owner in (("wt_" + "0" * 32, "alice"), (binding["worktree_id"], "bob")):
        with pytest.raises(WorktreeNotFoundError, match="unavailable"):
            service.inspect(identifier, owner_id=owner)


def test_source_git_metadata_symlink_is_rejected(managed):
    source, service = managed
    metadata = source.parent / "external-git-metadata"
    (source / ".git").rename(metadata)
    (source / ".git").symlink_to(metadata, target_is_directory=True)
    with pytest.raises(WorktreeError, match="symbolic link"):
        create(service, source)
