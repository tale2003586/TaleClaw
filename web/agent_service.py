"""Single-owner, durable RunState admission and isolated process execution.

run_state.json is the existing RunState snapshot, not a second job model.
stream.jsonl commits public events and state transitions before the snapshot;
request.json is a private input artifact. Only this supervisor writes root run
state. Workers own their runtime and send state projections through a pipe.
"""
from __future__ import annotations

from dataclasses import dataclass
import fcntl
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import signal
import stat
import threading
import time
from typing import Any, Callable
from uuid import uuid4

from runtime.trace.run_state import RunState, now_iso
from user_scope import (DEFAULT_USER_ID, DEFAULT_USER_ROLE, normalize_chat_id,
                        normalize_user_id, normalize_user_role, parse_web_session_id,
                        web_chat_id, web_session_id)
from web.run_worker import worker_entry, process_tree, stop_processes


TERMINAL = frozenset({'completed', 'stopped', 'failed', 'interrupted'})
ACTIVE = frozenset({'queued', 'running', 'cancelling'})
_META = 'background_execution'


class RunNotFoundError(LookupError):
    pass


class RunConflictError(RuntimeError):
    pass


class RunCapacityError(RuntimeError):
    pass


class RunServiceUnavailableError(RuntimeError):
    pass


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def _atomic(path: Path, value) -> None:
    temporary = path.with_name(path.name + '.' + uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(_json(value) + '\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass
class _Worker:
    process: Any
    receiver: Any
    cancel: Any
    descendants: dict[int, str]
    cancel_at: float | None = None
    result: dict | None = None
    forced: bool = False


class AgentService:
    def __init__(self, *, root: str | Path | None = None, max_workers: int = 2,
                 max_pending: int = 32, worker_target: str | None = None,
                 cancel_grace_seconds: float = 3.0,
                 session_deleter: Callable[[str], bool] | None = None) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parents[1] / '.runs'
        self.max_workers = max(1, int(max_workers))
        self.max_pending = max(1, int(max_pending))
        self.worker_target = worker_target or 'web.run_worker:run_agent'
        self.cancel_grace_seconds = max(0.05, float(cancel_grace_seconds))
        self._session_deleter = session_deleter
        self._condition = threading.Condition(threading.RLock())
        self._states: dict[str, RunState] = {}
        self._workers: dict[str, _Worker] = {}
        self._requests: dict[tuple[str, str], str] = {}
        self._thread: threading.Thread | None = None
        self._lock_fd: int | None = None
        self._started = False
        self._stopping = False
        self._fatal: BaseException | None = None
        self._context = multiprocessing.get_context('spawn')

    def ensure_started(self) -> None:
        with self._condition:
            if self._stopping or self._fatal:
                raise RunServiceUnavailableError('Background run service is stopping or unavailable.')
            if self._started:
                return
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise RunServiceUnavailableError('Run root must be a real directory.')
            fd = os.open(self.root / '.supervisor.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise RunServiceUnavailableError('Supervisor lock is not a regular file.')
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise RunServiceUnavailableError('Another supervisor owns this run directory.') from exc
                self._lock_fd = fd
                self._recover()
                self._started = True
                self._thread = threading.Thread(target=self._monitor, name='agent-run-supervisor', daemon=True)
                self._thread.start()
            except BaseException:
                self._lock_fd = None
                os.close(fd)
                raise

    def submit(self, *, session_id: str, content: str, request_id: str,
               user_id: str = DEFAULT_USER_ID, user_role: str = DEFAULT_USER_ROLE,
               workspace_root: str | None = None, display_content: str | None = None,
               attachments: list[dict] | None = None, attachment_paths: list[str] | None = None,
               thinking_enabled: bool = False, model_profile: str = '',
               coding_task: dict | None = None) -> dict:
        self.ensure_started()
        user_id, session_id = normalize_user_id(user_id), normalize_chat_id(session_id)
        user_role = normalize_user_role(user_role)
        if not isinstance(content, str) or not content.strip():
            raise ValueError('content is required')
        if not isinstance(request_id, str) or not request_id.strip() or len(request_id) > 200:
            raise ValueError('request_id must be a nonempty string of at most 200 characters')
        if attachment_paths is not None and (not isinstance(attachment_paths, list)
                or len(attachment_paths) > 5 or not all(isinstance(p, str) for p in attachment_paths)):
            raise ValueError('attachment_paths must contain at most five storage paths')
        request = dict(session_id=session_id, content=content, user_id=user_id, user_role=user_role,
                       workspace_root=workspace_root, display_content=display_content,
                       attachments=attachments, attachment_paths=attachment_paths,
                       thinking_enabled=bool(thinking_enabled), model_profile=model_profile,
                       coding_task=coding_task)
        # Serialize before admission, both to validate and detach caller-owned objects.
        request = json.loads(_json(request))
        digest = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._condition:
            self._available()
            previous = self._requests.get((user_id, request_id))
            if previous:
                state = self._states[previous]
                if state.metadata[_META]['request_sha256'] != digest:
                    raise RunConflictError('request_id was already used for a different request.')
                return self._dto(state)
            storage_id = web_session_id(user_id, session_id)
            for state in self._states.values():
                if state.session_id == storage_id and (state.status in ACTIVE or state.metadata[_META].get('quarantined')):
                    raise RunConflictError('This session has an active or quarantined run.')
                if state.metadata[_META].get('quarantined'):
                    binding = state.metadata.get('worktree_binding') or {}
                    previous_root = binding.get('workspace_root') or state.metadata.get('workspace_root')
                    if previous_root and workspace_root:
                        previous_path = Path(previous_root).expanduser().resolve()
                        requested_path = Path(workspace_root).expanduser().resolve()
                        if previous_path.is_relative_to(requested_path) or requested_path.is_relative_to(previous_path):
                            raise RunConflictError('This workspace overlaps a quarantined execution.')
                    elif workspace_root is None and not binding:
                        raise RunConflictError('An interrupted execution has an unconfirmed default workspace.')
            if sum(s.status in ACTIVE for s in self._states.values()) >= self.max_workers + self.max_pending:
                raise RunCapacityError('Background run capacity is full.')
            state = RunState.create(session_id=storage_id, channel='web', chat_id=web_chat_id(user_id, session_id),
                                    user_id=user_id, user_role=user_role, execution_path='queued')
            state.status = 'queued'
            state.metadata[_META] = dict(request_id=request_id, request_sha256=digest,
                                        client_session_id=session_id, created_at=state.started_at,
                                        updated_at=state.started_at, last_seq=0, attempt=1,
                                        request_ref='request.json', quarantined=False)
            if workspace_root:
                state.metadata['workspace_root'] = str(workspace_root)
            directory = self.root / state.run_id
            directory.mkdir(mode=0o700)
            _atomic(directory / 'request.json', request)
            self._states[state.run_id] = state
            self._requests[(user_id, request_id)] = state.run_id
            self._commit(state, {'type': 'status', 'status': 'queued'}, transition=True)
            self._condition.notify_all()
            return self._dto(state)

    def list_runs(self, *, user_id: str, session_id: str | None = None) -> list[dict]:
        self.ensure_started()
        user_id = normalize_user_id(user_id)
        if session_id is not None:
            session_id = normalize_chat_id(session_id)
        with self._condition:
            return [self._dto(state) for state in sorted(self._states.values(), key=lambda s: s.started_at, reverse=True)
                    if state.user_id == user_id and (session_id is None or state.metadata[_META]['client_session_id'] == session_id)]

    def get_run(self, run_id: str, *, user_id: str) -> dict:
        self.ensure_started()
        with self._condition:
            return self._dto(self._authorized(run_id, user_id))

    def events_after(self, run_id: str, *, user_id: str, after: int = 0,
                     wait_seconds: float = 0) -> list[dict]:
        self.ensure_started()
        if isinstance(after, bool) or not isinstance(after, int) or after < 0:
            raise ValueError('after must be a nonnegative integer')
        deadline = time.monotonic() + min(25.0, max(0.0, float(wait_seconds)))
        with self._condition:
            while True:
                state = self._authorized(run_id, user_id)
                if after > state.metadata[_META]['last_seq']:
                    raise ValueError('Event cursor is beyond the last committed event.')
                events = [record['event'] for record in self._read_journal(run_id)
                          if record['event']['seq'] > after][:1000]
                if events or state.status in TERMINAL or self._stopping or time.monotonic() >= deadline:
                    return events
                self._condition.wait(timeout=max(0, deadline - time.monotonic()))

    def cancel_run(self, run_id: str, *, user_id: str) -> dict:
        self.ensure_started()
        with self._condition:
            state = self._authorized(run_id, user_id)
            if state.status in TERMINAL:
                return self._dto(state)
            if state.status == 'queued':
                self._finish(state, 'stopped', reply='', reason='Cancelled before execution.')
            else:
                worker = self._workers.get(run_id)
                if worker:
                    worker.cancel.set()
                    if worker.cancel_at is None:
                        worker.cancel_at = time.monotonic()
                if state.status != 'cancelling':
                    state.status = 'cancelling'
                    self._commit(state, {'type': 'status', 'status': 'cancelling'}, transition=True)
            self._condition.notify_all()
            return self._dto(state)

    def ask(self, *, timeout: int = 180, **kwargs) -> str:
        return self.ask_stream(timeout=timeout, **kwargs)

    def ask_stream(self, *, on_text=None, timeout: int = 1800, request_id: str | None = None, **kwargs) -> str:
        run = self.submit(request_id=request_id or uuid4().hex, **kwargs)
        deadline, cursor = time.monotonic() + timeout, 0
        user_id = kwargs.get('user_id', DEFAULT_USER_ID)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Run continues in the background; the response wait timed out.')
            for event in self.events_after(run['run_id'], user_id=user_id, after=cursor, wait_seconds=min(remaining, 1)):
                cursor = event['seq']
                kind = event['type']
                if on_text is not None:
                    if kind == 'delta':
                        on_text(event.get('text', ''))
                    else:
                        callback = getattr(on_text, 'on_' + kind, None)
                        if callback:
                            callback(event.get('text', '') if kind == 'thinking' else event.get('data', {}))
                if kind == 'complete':
                    return event.get('reply', '')
                if kind == 'error':
                    raise RuntimeError(event.get('error', 'Background execution failed.'))

    def delete_session(self, session_id: str, timeout: int = 10) -> bool:
        self.ensure_started()
        if parse_web_session_id(session_id) is None:
            raise ValueError('Only scoped Web sessions may be deleted.')
        with self._condition:
            if any(s.session_id == session_id and (s.status in ACTIVE or s.metadata[_META].get('quarantined'))
                   for s in self._states.values()):
                raise RunConflictError('Cannot delete a session with an active or quarantined run.')
            if self._session_deleter:
                return bool(self._session_deleter(session_id))
            from runtime.sessions import SessionStore
            store = SessionStore()
            try:
                return store.delete_session(session_id)
            finally:
                store.close()

    def stop(self, timeout: float = 10) -> None:
        with self._condition:
            if not self._started:
                self._stopping = True
                return
            if not self._stopping:
                if not self._fatal:
                    for state in list(self._states.values()):
                        if state.status in ACTIVE:
                            self.cancel_run(state.run_id, user_id=str(state.user_id))
                self._stopping = True
                self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(timeout, self.cancel_grace_seconds + 2))
            if thread.is_alive():
                raise RunServiceUnavailableError('Supervisor has not confirmed worker shutdown.')

    def _available(self):
        if self._stopping or self._fatal:
            raise RunServiceUnavailableError('Background run service is unavailable.')

    def _authorized(self, run_id, user_id):
        user_id = normalize_user_id(user_id)
        state = self._states.get(str(run_id))
        if state is None or state.user_id != user_id:
            raise RunNotFoundError('Run not found.')
        return state

    def _dto(self, state):
        meta = state.metadata[_META]
        binding = state.metadata.get('worktree_binding') or {}
        result = dict(run_id=state.run_id, session_id=meta['client_session_id'],
                      storage_session_id=state.session_id, status=state.status,
                      created_at=meta['created_at'], updated_at=meta['updated_at'],
                      last_seq=meta['last_seq'], quarantined=bool(meta.get('quarantined')))
        if state.error:
            result['error'] = state.error
        if state.final_answer is not None:
            result['reply'] = state.final_answer
        task_id = state.metadata.get('task_id') or binding.get('task_id')
        workspace = binding.get('workspace_root') or state.metadata.get('workspace_root')
        if task_id:
            result['task_id'] = task_id
        if workspace:
            result['workspace_root'] = workspace
            result['workspace'] = (json.loads(_json(binding)) if binding else
                                   {'workspace_root': workspace, 'mode': 'in_place'})
        return result

    def _commit(self, state, event, *, transition=False):
        meta = state.metadata[_META]
        meta['last_seq'] += 1
        meta['updated_at'] = now_iso()
        event = {**event, 'seq': meta['last_seq'], 'run_id': state.run_id,
                 'session_id': meta['client_session_id'], 'timestamp': meta['updated_at']}
        record = {'event': event}
        if transition:
            record['run_state'] = state.to_dict()
        path = self.root / state.run_id / 'stream.jsonl'
        try:
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as handle:
                handle.write(_json(record) + '\n')
                handle.flush()
                os.fsync(handle.fileno())
            _atomic(self.root / state.run_id / 'run_state.json', state.to_dict())
        except BaseException as exc:
            self._fatal = exc
            self._condition.notify_all()
            raise
        self._condition.notify_all()

    def _read_journal(self, run_id, *, repair=False):
        path = self.root / run_id / 'stream.jsonl'
        if not path.exists():
            return []
        if path.is_symlink() or not path.is_file():
            raise RunServiceUnavailableError('Run event journal is not a regular file.')
        records = []
        data = path.read_bytes()
        valid_bytes = 0
        for line in data.splitlines(keepends=True):
            if not line.endswith(b'\n'):
                if repair:
                    with path.open('r+b') as handle:
                        handle.truncate(valid_bytes)
                        handle.flush()
                        os.fsync(handle.fileno())
                    break
                raise RunServiceUnavailableError('Incomplete run event journal.')
            record = json.loads(line)
            if record['event']['seq'] != len(records) + 1:
                raise RunServiceUnavailableError('Run event sequence is inconsistent.')
            records.append(record)
            valid_bytes += len(line)
        return records

    def _recover(self):
        for directory in sorted(self.root.glob('run_*')):
            if directory.is_symlink() or not directory.is_dir():
                continue
            path = directory / 'run_state.json'
            journal = directory / 'stream.jsonl'
            if not path.exists() and not journal.exists():
                continue
            raw = json.loads(path.read_text()) if path.is_file() and not path.is_symlink() else None
            records = self._read_journal(directory.name, repair=True)
            for record in records:
                if 'run_state' in record:
                    raw = record['run_state']
            if not isinstance(raw, dict) or _META not in raw.get('metadata', {}):
                continue  # Existing diagnostic-only runs are not dispatchable work.
            state = RunState(**raw)
            if state.run_id != directory.name:
                raise RunServiceUnavailableError('Run identity differs from its directory.')
            meta = state.metadata[_META]
            meta['last_seq'] = len(records)
            if records:
                meta['updated_at'] = records[-1]['event']['timestamp']
            self._states[state.run_id] = state
            self._requests[(str(state.user_id), meta['request_id'])] = state.run_id
            if state.status in ACTIVE:
                self._quarantine(state, 'Supervisor restarted before execution exit was confirmed.')
                self._finish(state, 'interrupted', reason='Previous execution was interrupted; it was not restarted.')
            else:
                _atomic(path, state.to_dict())

    def _launch(self, state):
        request = json.loads((self.root / state.run_id / 'request.json').read_text())
        request.update(run_id=state.run_id, run_root=str(self.root), attempt=state.metadata[_META]['attempt'])
        receiver, sender = self._context.Pipe(duplex=False)
        cancelled = self._context.Event()
        process = self._context.Process(target=worker_entry, args=(self.worker_target, request, sender, cancelled),
                                        name='agent-worker-' + state.run_id)
        try:
            process.start()
        except Exception as exc:
            receiver.close()
            sender.close()
            self._finish(state, 'failed', reason=f'Worker could not start: {type(exc).__name__}: {exc}')
            return
        sender.close()
        self._workers[state.run_id] = _Worker(process, receiver, cancelled, {})
        state.status = 'running'
        state.execution_path = 'isolated_worker'
        state.metadata[_META]['worker_pid'] = process.pid
        self._commit(state, {'type': 'status', 'status': 'running'}, transition=True)

    def _accept(self, state, worker, message):
        if message.get('run_id') != state.run_id or message.get('attempt') != state.metadata[_META]['attempt']:
            return
        kind = message.get('type')
        if kind == '_result':
            worker.result = message.get('result') or {}
        elif kind == '_state':
            snapshot = message.get('state') or {}
            for name in ('mode', 'execution_path', 'reasoning_steps', 'tool_calls', 'last_tool', 'intent', 'agent'):
                if name in snapshot:
                    setattr(state, name, snapshot[name])
            # Root lifecycle remains exclusively supervised; worker metadata is evidence.
            state.metadata.update({k: v for k, v in (snapshot.get('metadata') or {}).items() if k != _META})
            self._commit(state, {'type': 'status', 'status': state.status,
                                 **{key: val for key, val in self._dto(state).items() if key in {'task_id', 'workspace', 'workspace_root'}}},
                         transition=True)
        elif kind in {'delta', 'thinking', 'event', 'status', 'assistant_segment', 'assistant_completed'}:
            event = {key: value for key, value in message.items() if key not in {'run_id', 'attempt'}}
            self._commit(state, event)

    def _drain(self, state, worker):
        while worker.receiver.poll():
            try:
                message = worker.receiver.recv()
            except EOFError:
                break
            self._accept(state, worker, message)

    def _quarantine(self, state, reason):
        state.metadata[_META]['quarantined'] = True
        state.metadata[_META]['quarantine_reason'] = reason
        binding = state.metadata.get('worktree_binding') or {}
        if binding.get('worktree_id'):
            try:
                from runtime.worktrees import WorktreeService
                WorktreeService(root=Path(binding['service_root']) if binding.get('service_root') else None).quarantine(binding['worktree_id'], reason)
            except Exception as exc:
                # Never release the session merely because quarantine I/O failed.
                state.metadata[_META]['quarantine_error'] = f'{type(exc).__name__}: {exc}'

    def _finish(self, state, status, *, reply=None, reason=None):
        state.finish(reply, status=status, stop_reason=reason)
        if status in {'failed', 'interrupted'}:
            state.error = reason or 'Worker failed.'
            event = {'type': 'error', 'status': status, 'error': state.error}
        else:
            event = {'type': 'complete', 'status': status, 'reply': reply or ''}
        self._commit(state, event, transition=True)

    def _tick(self):
        if self._fatal:
            raise self._fatal
        for run_id, worker in list(self._workers.items()):
            state = self._states[run_id]
            worker.descendants.update(process_tree(worker.process.pid))
            self._drain(state, worker)
            if worker.cancel_at is not None and time.monotonic() - worker.cancel_at >= self.cancel_grace_seconds and worker.process.is_alive():
                worker.forced = True
                self._quarantine(state, 'Cancellation required forcibly stopping the worker.')
                stop_processes(worker.descendants, include=worker.process.pid)
                worker.process.kill()
            if worker.process.is_alive():
                continue
            worker.process.join(timeout=0)
            self._drain(state, worker)
            descendants_exited = stop_processes(worker.descendants)
            result = worker.result
            if worker.forced or worker.process.exitcode != 0 or not result or not result.get('cleanup_confirmed', False) or not descendants_exited:
                self._quarantine(state, 'Worker exit or descendant completion was not normal.')
                reason = 'Worker exited without confirmed normal cleanup; workspace quarantined.'
                if result and result.get('error'):
                    reason = str(result['error']) + '; ' + reason
                self._finish(state, 'interrupted', reason=reason)
            else:
                status = result.get('status', 'completed')
                if status not in {'completed', 'stopped', 'failed'}:
                    status = 'failed'
                if worker.cancel.is_set() and status == 'completed':
                    status = 'stopped'
                self._finish(state, status, reply=result.get('reply', ''), reason=result.get('error'))
            worker.receiver.close()
            worker.process.close()
            del self._workers[run_id]
        if not self._stopping:
            for state in list(self._states.values()):
                if len(self._workers) >= self.max_workers:
                    break
                if state.status == 'queued':
                    self._launch(state)

    def _monitor(self):
        try:
            with self._condition:
                while True:
                    self._tick()
                    if self._stopping and not self._workers:
                        return
                    self._condition.wait(timeout=0.025)
        except BaseException as exc:
            with self._condition:
                self._fatal = exc
                # No further admissions. Best-effort stop, preserve unknown ownership.
                for run_id, worker in list(self._workers.items()):
                    worker.cancel.set()
                    worker.descendants.update(process_tree(worker.process.pid))
                    stop_processes(worker.descendants, include=worker.process.pid)
                    if worker.process.is_alive():
                        worker.process.kill()
                    worker.process.join(timeout=1)
                    state = self._states[run_id]
                    try:
                        self._quarantine(state, 'Supervisor failed.')
                        self._finish(state, 'interrupted', reason='Supervisor failed; execution requires inspection.')
                    except Exception:
                        pass
                self._condition.notify_all()
        finally:
            with self._condition:
                if self._lock_fd is not None:
                    os.close(self._lock_fd)
                    self._lock_fd = None
                self._condition.notify_all()
