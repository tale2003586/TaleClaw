"""Owned Git workspaces. Advisory coordination, never an OS security sandbox."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import venv
from uuid import uuid4

from config import WORKDIR
from runtime.workspace_lease import managed_workspace_writer, WorkspaceBusyError


class WorktreeError(RuntimeError):
    pass


class WorktreeNotFoundError(WorktreeError):
    pass


class WorktreeQuarantinedError(WorktreeError):
    pass


def git_environment() -> dict[str, str]:
    # Do not inherit hooks' repository locators, index overrides or config injection.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1")
    return env


def git_run(cwd: Path, args: list[str], *, check: bool = True, timeout: float = 60):
    command = ["git", "-c", f"core.hooksPath={os.devnull}", "-c", "protocol.file.allow=never"]
    config = subprocess.run([*command, "config", "--get-regexp", r"^(filter\..*\.(clean|smudge|process)|merge\..*\.driver|core\.fsmonitor)$"],
                            cwd=cwd, env=git_environment(), capture_output=True, text=True, timeout=timeout)
    if config.returncode not in {0, 1} or config.stdout.strip():
        raise WorktreeError("External Git filters, merge drivers and fsmonitor commands are not supported")
    if args and args[0] == "diff":
        args = ["diff", "--no-ext-diff", "--no-textconv", *args[1:]]
    result = subprocess.run(
        [*command, *args],
        cwd=cwd, env=git_environment(), capture_output=True, text=True, timeout=timeout,
    )
    if check and result.returncode:
        raise WorktreeError(f"Git operation failed ({result.returncode}): {result.stderr.strip()[:2000]}")
    return result


def resolve_commit(source_root: Path, ref: str = "HEAD") -> str:
    if not isinstance(ref, str) or not ref or len(ref) > 256 or ref.startswith("-") or any(ord(c) < 32 for c in ref):
        raise ValueError("base_ref must be a Git commit reference")
    commit = git_run(source_root, ["rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"]).stdout.strip()
    if not re.fullmatch(r"[a-f0-9]{40}|[a-f0-9]{64}", commit):
        raise WorktreeError("Git did not resolve a fixed commit")
    return commit


def _real_path(value: Path) -> Path:
    path = Path(value).expanduser().absolute()
    if path != path.resolve():
        raise WorktreeError("Managed paths must not contain symbolic links or aliases")
    return path


def _atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _file_lock(path: Path, *, blocking: bool = False):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise WorkspaceBusyError("Managed resource is busy") from exc
        yield
    finally:
        os.close(fd)


class WorktreeService:
    def __init__(self, root: Path | None = None):
        self.root = _real_path(Path(root or os.getenv("MANAGED_WORKTREE_ROOT") or
                                   WORKDIR.parent / f".{WORKDIR.name}-worktrees"))

    def _initialize(self):
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = _real_path(self.root).stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WorktreeError("Managed root must be private and owned by this service user")
        for name in ("registry", "checkouts", "leases", "runtime"):
            child = _real_path(self.root / name)
            child.mkdir(mode=0o700, exist_ok=True)
            info = child.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise WorktreeError("Managed storage directories must be private")

    def _record_path(self, worktree_id: str) -> Path:
        if not isinstance(worktree_id, str) or not re.fullmatch(r"wt_[a-f0-9]{32}", worktree_id):
            raise ValueError("Invalid worktree_id")
        return _real_path(self.root / "registry" / f"{worktree_id}.json")

    def _read(self, worktree_id: str, owner_id=None) -> dict:
        try:
            binding = json.loads(self._record_path(worktree_id).read_text())
        except FileNotFoundError as exc:
            raise WorktreeNotFoundError("Managed worktree is unavailable") from exc
        if owner_id is not None and binding.get("owner_id") != owner_id:
            raise WorktreeNotFoundError("Managed worktree is unavailable")
        if binding.get("worktree_id") != worktree_id or binding.get("service_root") != str(self.root):
            raise WorktreeError("Invalid worktree ownership record")
        return binding

    def _save(self, binding):
        _atomic_json(self._record_path(binding["worktree_id"]), binding)

    @staticmethod
    def _identity(source_root: Path) -> tuple[Path, Path]:
        source = _real_path(source_root)
        if (source / ".git").is_symlink():
            raise WorktreeError("Repository Git metadata must not be a symbolic link")
        top = Path(git_run(source, ["rev-parse", "--show-toplevel"]).stdout.strip())
        if top.resolve() != source:
            raise WorktreeError("Source must be an authorized repository checkout root")
        common = _real_path(Path(git_run(source, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip()))
        return source, common

    def create(self, source_root: Path, *, task_id: str, owner_id: str, base_ref: str | None = None) -> dict:
        if not task_id or not owner_id:
            raise ValueError("task_id and owner_id are required")
        source, common = self._identity(Path(source_root))
        if self.root == source or self.root.is_relative_to(source) or source.is_relative_to(self.root):
            raise WorktreeError("Managed storage and source checkout must not overlap")
        # A checkout can execute configured smudge/process commands. Unsupported
        # filters are rejected rather than running repository-controlled programs.
        filters = git_run(source, ["config", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"], check=False)
        if filters.returncode not in {0, 1} or filters.stdout.strip():
            raise WorktreeError("Repositories with external Git filters are not supported")
        base = resolve_commit(source, base_ref or "HEAD")
        dirty = bool(git_run(source, ["status", "--porcelain", "--untracked-files=all"]).stdout)
        self._initialize()
        common_stat = common.stat()
        repository_identity = {"device": common_stat.st_dev, "inode": common_stat.st_ino}
        repo_id = "repo_" + hashlib.sha256(
            json.dumps([str(common), repository_identity], sort_keys=True).encode()).hexdigest()[:32]
        worktree_id = "wt_" + uuid4().hex
        destination = self.root / "checkouts" / worktree_id
        binding = dict(worktree_id=worktree_id, repo_id=repo_id, service_root=str(self.root),
                       source_root=str(source), common_git_dir=str(common), workspace_root=str(destination),
                       repository_identity=repository_identity,
                       branch=f"taleclaw/{worktree_id}", base_commit=base, owner_id=owner_id,
                       task_id=task_id, status="creating", dirty_source_excluded=dirty, preserved_refs=[])
        with _file_lock(self.root / "leases" / f"{repo_id}.lock", blocking=True):
            self._save(binding)
            try:
                git_run(source, ["worktree", "add", "-b", binding["branch"], str(destination), base])
                binding["status"] = "ready"
                self._save(binding)
                return self.inspect(worktree_id, owner_id=owner_id)
            except BaseException:
                binding.update(status="quarantined", quarantine_reason="Workspace creation did not complete")
                self._save(binding)
                raise

    def inspect(self, worktree_id: str, *, owner_id=None) -> dict:
        binding = self._read(worktree_id, owner_id)
        expected = self.root / "checkouts" / worktree_id
        if binding["workspace_root"] != str(expected):
            raise WorktreeError("Worktree path does not match its ownership record")
        if binding["status"] == "removed":
            return binding
        path = _real_path(expected)
        if not path.is_dir() or not (path / ".git").is_file() or (path / ".git").is_symlink():
            raise WorktreeError("Managed checkout is missing or has invalid Git identity")
        _, common = self._identity(path)
        branch = git_run(path, ["symbolic-ref", "--quiet", "HEAD"], check=False).stdout.strip()
        common_stat = common.stat()
        identity = {"device": common_stat.st_dev, "inode": common_stat.st_ino}
        if (str(common) != binding["common_git_dir"] or identity != binding.get("repository_identity")
                or branch != "refs/heads/" + binding["branch"]):
            raise WorktreeError("Managed checkout Git identity changed")
        return {**binding, "head_commit": resolve_commit(path),
                "dirty": bool(git_run(path, ["status", "--porcelain", "--untracked-files=all"]).stdout)}

    def list(self, *, owner_id=None, repo_id=None) -> list[dict]:
        if not self.root.exists():
            return []
        result = []
        for path in sorted((self.root / "registry").glob("wt_*.json")):
            binding = self._read(path.stem)
            if (owner_id is None or binding["owner_id"] == owner_id) and (repo_id is None or binding["repo_id"] == repo_id):
                try:
                    binding = self.inspect(path.stem, owner_id=owner_id)
                except WorktreeError as exc:
                    binding = {**binding, "inspection_error": str(exc)}
                result.append(binding)
        return result

    @contextmanager
    def lease(self, binding: dict):
        current = self.inspect(binding["worktree_id"], owner_id=binding["owner_id"])
        with managed_workspace_writer(current["workspace_root"], self.root / "leases" / f"{current['worktree_id']}.lock"):
            current = self._read(current["worktree_id"], binding["owner_id"])
            if current["status"] != "ready":
                raise WorktreeQuarantinedError("Workspace is active, incomplete or quarantined; recovery is required")
            current.update(status="active", active_pid=os.getpid())
            self._save(current)
            try:
                yield self.inspect(current["worktree_id"], owner_id=current["owner_id"])
            except BaseException:
                self.quarantine(current["worktree_id"], "Writer did not complete cleanly")
                raise
            else:
                latest = self._read(current["worktree_id"])
                if latest["status"] == "active":
                    latest.update(status="ready")
                    latest.pop("active_pid", None)
                    self._save(latest)

    def quarantine(self, worktree_id: str, reason: str):
        binding = self._read(worktree_id)
        binding.update(status="quarantined", quarantine_reason=str(reason)[:1000])
        self._save(binding)
        return binding

    def preserve(self, worktree_id: str, *, owner_id: str, ref: str, commit: str) -> dict:
        if not re.fullmatch(r"refs/taleclaw/deliveries/[A-Za-z0-9_-]+", ref):
            raise ValueError("A dedicated delivery ref is required")
        binding = self.inspect(worktree_id, owner_id=owner_id)
        path = Path(binding["workspace_root"])
        if resolve_commit(path, ref) != commit:
            raise WorktreeError("Preservation ref does not identify the supplied commit")
        with _file_lock(self.root / "leases" / f"{worktree_id}.lock"):
            binding = self._read(worktree_id, owner_id)
            if binding["status"] != "ready":
                raise WorktreeError("Cannot preserve an active or quarantined workspace")
            binding["preserved_refs"] = [*binding.get("preserved_refs", []), {"ref": ref, "commit": commit}]
            self._save(binding)
        return self.inspect(worktree_id, owner_id=owner_id)

    def remove(self, worktree_id: str, *, owner_id: str, preserved_commit=None) -> dict:
        # Client-provided hashes are never proof of preservation.
        with _file_lock(self.root / "leases" / f"{worktree_id}.lock"):
            binding = self.inspect(worktree_id, owner_id=owner_id)
            if binding["status"] != "ready" or binding["dirty"]:
                raise WorktreeError("Active, quarantined, dirty or untracked workspaces cannot be removed")
            path = Path(binding["workspace_root"])
            head = binding["head_commit"]
            preserved = head == binding["base_commit"]
            for item in binding.get("preserved_refs", []):
                if (resolve_commit(path, item["ref"]) == item["commit"] and
                        git_run(path, ["merge-base", "--is-ancestor", head, item["commit"]], check=False).returncode == 0):
                    preserved = True
            if not preserved:
                raise WorktreeError("Unpreserved task commits must be delivered before removal")
            with _file_lock(self.root / "leases" / f"{binding['repo_id']}.lock", blocking=True):
                git_run(Path(binding["source_root"]), ["worktree", "remove", str(path)])
                # Keep the branch as a recovery ref; no forced branch deletion.
                binding.update(status="removed")
                self._save(binding)
                return binding

    def command_environment(self, binding: dict) -> dict[str, str]:
        current = self.inspect(binding["worktree_id"], owner_id=binding["owner_id"])
        directory = self.root / "runtime" / current["worktree_id"]
        for name in ("tmp", "cache", "home"):
            _real_path(directory / name).mkdir(mode=0o700, parents=True, exist_ok=True)
        # Host model/DB/cloud credentials and service endpoints are intentionally
        # absent. Explicit task-specific service setup can be added by the host.
        env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "TZ", "TERM", "SYSTEMROOT") if key in os.environ}
        python_env = _real_path(directory / "venv")
        if not (python_env / "pyvenv.cfg").is_file():
            venv.EnvBuilder(with_pip=True).create(python_env)
        env["PATH"] = str(python_env / "bin") + ":/usr/local/bin:/usr/bin:/bin"
        env["VIRTUAL_ENV"] = str(python_env)
        env.update(PYTHONDONTWRITEBYTECODE="1", PIP_DISABLE_PIP_VERSION_CHECK="1",
                   HOME=str(directory / "home"), TMPDIR=str(directory / "tmp"),
                   XDG_CACHE_HOME=str(directory / "cache"), PYTHON_DOTENV_DISABLED="1",
                   GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0",
                   COMPOSE_PROJECT_NAME="taleclaw_" + current["worktree_id"])
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            env["PORT"] = str(probe.getsockname()[1])
        return env


    @contextmanager
    def command_scope(self, binding: dict):
        """Serialize managed shell effects; isolated env is not network confinement.

        A durable uncertainty marker prevents the next command from overlapping
        descendants whose exit has not been confirmed. No automatic timeout reset.
        """
        lease_root = _real_path(Path(os.getenv("TALECLAW_RESOURCE_LOCK_ROOT") or
                                    Path(tempfile.gettempdir()) / f"taleclaw-command-leases-{os.getuid()}"))
        lease_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = lease_root.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WorktreeError("Command lease directory must be private")
        marker = lease_root / "active.json"
        # Waiting here would bypass command timeout/cancellation checks.
        # A busy resource can be retried without starting shell effects.
        with _file_lock(lease_root / "command.lock"):
            if marker.exists():
                raise WorktreeQuarantinedError("Previous command exit is unconfirmed; resource recovery is required")
            current = self.inspect(binding["worktree_id"], owner_id=binding["owner_id"])
            if current["status"] not in {"ready", "active"}:
                raise WorktreeQuarantinedError("Workspace cannot execute commands")
            _atomic_json(marker, {"worktree_id": current["worktree_id"], "service_root": str(self.root),
                                  "pid": os.getpid()})
            completion = {"confirmed": False}
            try:
                yield self.command_environment(current), completion
            finally:
                if completion["confirmed"]:
                    marker.unlink()
                else:
                    self.quarantine(current["worktree_id"], "Command descendants or resource release unconfirmed")
