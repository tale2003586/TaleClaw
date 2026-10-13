"""Versioned delivery evidence for owned worktrees; never reset a user checkout."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from runtime.worktrees import (
    WorktreeError, WorktreeService, _atomic_json, _file_lock, _real_path,
    git_run, resolve_commit,
)


class DeliveryConflictError(WorktreeError):
    pass


class DeliveryNotFoundError(WorktreeError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _operation_owner() -> dict:
    return {"pid": os.getpid(), "started": _process_started(os.getpid())}


def _process_started(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
    except (OSError, IndexError, ValueError):
        return ""


class WorktreeDeliveryService:
    def __init__(self, worktrees: WorktreeService | None = None):
        self.worktrees = worktrees or WorktreeService()
        self.root = self.worktrees.root / "deliveries"

    def _initialize(self) -> None:
        self.worktrees._initialize()
        _real_path(self.root).mkdir(mode=0o700, exist_ok=True)

    def _path(self, identifier: str) -> Path:
        if not isinstance(identifier, str) or not re.fullmatch(r"dl_[a-f0-9]{32}", identifier):
            raise ValueError("Invalid delivery id")
        return _real_path(self.root / f"{identifier}.json")

    def _read(self, identifier: str, owner_id: str) -> dict:
        try:
            value = json.loads(self._path(identifier).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise DeliveryNotFoundError("Delivery not found") from exc
        if value.get("id") != identifier or value.get("owner_id") != owner_id:
            raise DeliveryNotFoundError("Delivery not found")
        return value

    def _save(self, value: dict) -> None:
        value["updated_at"] = _now()
        _atomic_json(self._path(value["id"]), value)

    def checkpoint(self, worktree_id: str, *, owner_id: str) -> dict:
        binding = self.worktrees.inspect(worktree_id, owner_id=owner_id)
        if binding["status"] != "ready":
            raise DeliveryConflictError("Stop the task before preserving its changes")
        with self.worktrees.lease(binding) as active:
            path = Path(active["workspace_root"])
            git_run(path, ["add", "--all", "--", "."])
            changed = git_run(path, ["diff", "--cached", "--quiet"], check=False)
            if changed.returncode not in {0, 1}:
                raise WorktreeError("Cannot inspect the task index")
            if changed.returncode == 1:
                self._commit(path, f"Task checkpoint {active['task_id']}")
            commit = resolve_commit(path)
            tree = git_run(path, ["rev-parse", f"{commit}^{{tree}}"] ).stdout.strip()
            ref = f"refs/taleclaw/deliveries/checkpoint_{uuid4().hex}"
            git_run(path, ["update-ref", ref, commit, "0" * len(commit)])
        self.worktrees.preserve(worktree_id, owner_id=owner_id, ref=ref, commit=commit)
        return {"worktree_id": worktree_id, "commit": commit, "tree": tree,
                "ref": ref, "created_at": _now()}

    def prepare(self, worktree_id: str, *, owner_id: str, target_ref: str | None = None) -> dict:
        source = self.worktrees.inspect(worktree_id, owner_id=owner_id)
        if source["status"] != "ready" or source["dirty"]:
            raise DeliveryConflictError("Stop the task and checkpoint its changes before integration")
        source_root = Path(source["source_root"])
        if target_ref is None:
            target_ref = git_run(source_root, ["symbolic-ref", "--quiet", "HEAD"], check=False).stdout.strip()
            if not target_ref:
                raise ValueError("A target branch is required for a detached source checkout")
        self._validate_target(source_root, target_ref)
        target_sha = resolve_commit(source_root, target_ref)
        # Fix the source input under the same lease used by task writers. No
        # later stage reads the moving task branch to decide what to integrate.
        dirty = False
        with self.worktrees.lease(source) as active:
            dirty = bool(active["dirty"])
            source_sha = active["head_commit"]
        if dirty:
            raise DeliveryConflictError("Task changes appeared before its checkpoint was fixed")
        self._initialize()
        identifier = f"dl_{uuid4().hex}"
        value = {"id": identifier, "owner_id": owner_id,
                 "source_worktree_id": worktree_id, "task_id": source["task_id"],
                 "repo_id": source["repo_id"], "source_root": str(source_root),
                 "source_sha": source_sha, "target_ref": target_ref, "target_sha": target_sha,
                 "candidate_sha": "", "status": "preparing", "created_at": _now(),
                 "verification": None, "conflicts": [], "operation_owner": _operation_owner()}
        self._save(value)
        try:
            integration = self.worktrees.create(
                source_root, task_id=identifier, owner_id=owner_id, base_ref=target_sha,
            )
            value.update(worktree_id=integration["worktree_id"],
                         workspace_root=integration["workspace_root"], branch=integration["branch"])
            self._save(value)
            with self.worktrees.lease(integration):
                path = Path(integration["workspace_root"])
                merged = git_run(path, ["-c", "user.name=TaleClaw", "-c", "user.email=agent@localhost",
                                       "-c", "commit.gpgSign=false", "merge", "--no-ff", "--no-commit",
                                       "--no-edit", source_sha], check=False)
                conflicts = git_run(path, ["diff", "--name-only", "--diff-filter=U", "-z"]).stdout
                if merged.returncode and conflicts:
                    value.update(status="conflict", conflicts=[item for item in conflicts.split("\0") if item],
                                 error="Resolve the source task against the target and prepare a new candidate")
                elif merged.returncode:
                    raise WorktreeError(f"Candidate merge failed: {merged.stderr[:2000]}")
                else:
                    merge_head = git_run(path, ["rev-parse", "--verify", "MERGE_HEAD"], check=False)
                    if merge_head.returncode == 0:
                        self._commit(path, f"Integration candidate {identifier}")
                    candidate = resolve_commit(path)
                    ref = f"refs/taleclaw/deliveries/{identifier}"
                    git_run(path, ["update-ref", ref, candidate, "0" * len(candidate)])
                    value.update(status="prepared", candidate_sha=candidate, ref=ref,
                                 candidate_tree=git_run(path, ["rev-parse", f"{candidate}^{{tree}}"] ).stdout.strip())
            if value["status"] == "prepared":
                self.worktrees.preserve(integration["worktree_id"], owner_id=owner_id,
                                        ref=value["ref"], commit=value["candidate_sha"])
            self._save(value)
            return self.get(identifier, owner_id=owner_id)
        except BaseException as exc:
            value.update(status="failed", error=str(exc)[:2000])
            self._save(value)
            raise

    def get(self, identifier: str, *, owner_id: str) -> dict:
        value = self._read(identifier, owner_id)
        if value["status"] in {"preparing", "verifying"}:
            owner = value.get("operation_owner") or {}
            if not owner.get("started") or _process_started(owner.get("pid", 0)) != owner["started"]:
                # Display interrupted ownership; never infer rollback, release a
                # stale writer marker or replay a verification command here.
                value.update(status="interrupted", error="The operation process exited. Inspect the retained workspace and prepare a new candidate; execution is not replayed automatically.")
        if value["status"] in {"prepared", "verified", "published"}:
            try:
                current = resolve_commit(Path(value["source_root"]), value["target_ref"])
                value["target_current_sha"] = current
                if current != value["target_sha"]:
                    value.update(status="stale", error="The target advanced; prepare and verify a new candidate")
            except (WorktreeError, OSError):
                value.update(status="stale", error="The target can no longer be verified")
        return value

    def list(self, *, owner_id: str, worktree_id: str | None = None) -> list[dict]:
        if not self.root.exists():
            return []
        values = []
        for path in self.root.glob("dl_*.json"):
            try:
                value = self.get(path.stem, owner_id=owner_id)
            except DeliveryNotFoundError:
                continue
            if worktree_id is None or value["source_worktree_id"] == worktree_id:
                values.append(value)
        return sorted(values, key=lambda value: value["created_at"], reverse=True)

    def verify(self, identifier: str, *, owner_id: str, command: str, timeout_seconds: int = 120) -> dict:
        from tools.command import run_command

        if not isinstance(command, str) or not command.strip() or len(command) > 8000:
            raise ValueError("A verification command of at most 8000 characters is required")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 300:
            raise ValueError("timeout_seconds must be an integer from 1 to 300")
        record_path = self._path(identifier)
        self._initialize()
        with _file_lock(record_path.with_suffix(".lock")):
            value = self.get(identifier, owner_id=owner_id)
            if value["status"] not in {"prepared", "verified", "failed"} or not value["candidate_sha"]:
                raise DeliveryConflictError("Only a current, prepared candidate can be verified")
            binding = self.worktrees.inspect(value["worktree_id"], owner_id=owner_id)
            if (binding["status"] != "ready" or binding["dirty"]
                    or binding["head_commit"] != value["candidate_sha"]):
                raise DeliveryConflictError("The candidate workspace changed; prepare a new candidate")
            from tools.executor import ToolExecutionRequest
            from tools.hooks import ShellSafetyHook, ShellWorkspaceScopeHook
            request = ToolExecutionRequest(
                call_id=f"verify:{identifier}", tool_name="bash", arguments={"command": command},
                metadata={"workspace_root": binding["workspace_root"]},
            )
            for hook in (ShellSafetyHook(), ShellWorkspaceScopeHook(Path(binding["workspace_root"]))):
                decision = hook.before(request)
                if decision.deny_reason:
                    raise ValueError(decision.deny_reason)
            value.update(status="verifying", error=None, operation_owner=_operation_owner())
            value["verification"] = {"command": command, "timeout_seconds": timeout_seconds,
                                     "source_sha": value["source_sha"], "target_sha": value["target_sha"],
                                     "candidate_sha": value["candidate_sha"], "started_at": _now(),
                                     "status": "running", "environment": "managed private command environment"}
            self._save(value)
            try:
                with self.worktrees.lease(binding):
                    result = run_command(command, cwd=binding["workspace_root"],
                                         managed_binding=binding, timeout_seconds=timeout_seconds)
                    after = self.worktrees.inspect(binding["worktree_id"], owner_id=owner_id)
                    unchanged = not after["dirty"] and after["head_commit"] == value["candidate_sha"]
                    evidence = value["verification"]
                    evidence.update(status=result.status, exit_code=(result.metadata or {}).get("exit_code"),
                                    output=str(result), finished_at=_now(), source_unchanged=unchanged,
                                    error_type=result.error_type, effect_state=result.effect_state,
                                    environment=(result.metadata or {}).get("managed_environment", {}))
                    evidence["passed"] = result.status == "success" and evidence["exit_code"] == 0 and unchanged
                    value["status"] = "verified" if evidence["passed"] else "failed"
                    if not unchanged:
                        value["error"] = "Verification changed candidate sources; prepare and verify a new candidate"
                self._save(value)
                return self.get(identifier, owner_id=owner_id)
            except BaseException as exc:
                value.update(status="failed", error=str(exc)[:2000])
                value["verification"].update(status="interrupted", finished_at=_now(), passed=False)
                self._save(value)
                raise

    def publish(self, identifier: str, *, owner_id: str) -> dict:
        """Expose a verified candidate branch. The user's target is untouched."""
        record_path = self._path(identifier)
        self._initialize()
        with _file_lock(record_path.with_suffix(".lock")):
            value = self.get(identifier, owner_id=owner_id)
            if value["status"] == "published":
                return value
            if value["status"] != "verified" or not (value.get("verification") or {}).get("passed"):
                raise DeliveryConflictError("A current verified candidate is required")
            source = Path(value["source_root"])
            # This branch is generated from the owned delivery identity. No
            # caller-supplied branch/ref is ever updated here.
            ref = f"refs/heads/taleclaw/deliveries/{identifier}"
            if resolve_commit(source, value["ref"]) != value["candidate_sha"]:
                raise DeliveryConflictError("The preserved candidate ref changed")
            exists = git_run(source, ["rev-parse", "--verify", ref], check=False)
            if exists.returncode == 0:
                if exists.stdout.strip() != value["candidate_sha"]:
                    raise DeliveryConflictError("Delivery branch already exists with different contents")
            else:
                # Transaction checks the target again while creating the new
                # branch. A target move cannot silently publish old evidence.
                transaction = f"verify {value['target_ref']} {value['target_sha']}\ncreate {ref} {value['candidate_sha']}\n"
                self._update_refs(source, transaction)
            value.update(status="published", published_ref=ref, published_at=_now())
            self._save(value)
            return self.get(identifier, owner_id=owner_id)

    @staticmethod
    def _update_refs(source: Path, transaction: str) -> None:
        import subprocess
        from runtime.worktrees import git_environment
        result = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "update-ref", "--stdin"],
                                cwd=source, env=git_environment(), input=transaction,
                                capture_output=True, text=True, timeout=20)
        if result.returncode:
            raise DeliveryConflictError("Target changed while publishing; prepare a new candidate")

    @staticmethod
    def _commit(path: Path, message: str) -> None:
        git_run(path, ["-c", "user.name=TaleClaw", "-c", "user.email=agent@localhost",
                       "-c", "commit.gpgSign=false", "commit", "--no-verify", "-m", message])

    @staticmethod
    def _validate_target(source: Path, target_ref: str) -> None:
        if (not isinstance(target_ref, str) or not target_ref.startswith("refs/heads/")
                or len(target_ref) > 256 or any(ord(c) < 32 for c in target_ref)
                or git_run(source, ["check-ref-format", target_ref], check=False).returncode):
            raise ValueError("target_ref must be an existing full refs/heads/ branch")
