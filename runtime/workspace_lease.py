"""Cross-process, fail-fast coding writer lease.

The personal/small-team profile permits one writer per OS user, including
ancestor/descendant roots selected by different WorkspaceResolver instances.
"""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path
import tempfile


class WorkspaceBusyError(RuntimeError):
    pass


@contextmanager
def workspace_writer(root, *, _shared=False):
    canonical = str(Path(root).resolve(strict=True))
    lock_dir = Path(tempfile.gettempdir()) / f"taleclaw-workspace-leases-{os.getuid()}"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    info = lock_dir.lstat()
    if lock_dir.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Workspace lease directory is not private.")
    # A per-path key is unsound for overlapping roots. A conservative global
    # writer is cheaper and safer than maintaining a stale range-lock registry.
    path = lock_dir / "coding-writer.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, (fcntl.LOCK_SH if _shared else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorkspaceBusyError(f"A coding workspace writer is already active; requested: {canonical}") from exc
        yield canonical
    finally:
        # Do not unlink: a concurrent opener must always lock the same inode.
        os.close(fd)


@contextmanager
def managed_workspace_writer(root, lock_path):
    """Only WorktreeService calls this after checking its ownership registry.

    Shared acquisition of the old global lease keeps arbitrary in-place roots
    mutually exclusive with managed runs, while managed siblings can proceed.
    """
    with workspace_writer(root, _shared=True):
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkspaceBusyError("Managed workspace already has a writer") from exc
            yield str(Path(root).resolve(strict=True))
        finally:
            os.close(fd)
