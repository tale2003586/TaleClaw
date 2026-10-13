"""Spawn-only worker boundary. No runtime/config imports in the supervisor."""
from __future__ import annotations

import asyncio
import ctypes
from dataclasses import replace
import importlib
import os
from pathlib import Path
import signal
import threading
import time
from urllib.parse import unquote

from user_scope import web_chat_id, web_session_id, storage_root_for_user


def _process(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(') ', 1)[1].split()
        return int(fields[1]), fields[19], fields[0]
    except (OSError, ValueError, IndexError):
        return None


def process_tree(root_pid: int) -> dict[int, str]:
    """Linux descendant identity includes starttime, so PID reuse is not killed."""
    table = {}
    for entry in Path('/proc').iterdir():
        if entry.name.isdigit():
            info = _process(int(entry.name))
            if info:
                table[int(entry.name)] = info
    parents = {root_pid}
    result = {}
    while True:
        added = {pid for pid, (ppid, _, _) in table.items() if ppid in parents and pid not in parents}
        if not added:
            break
        for pid in added:
            result[pid] = table[pid][1]
        parents.update(added)
    return result


def _pidfd_open(pid: int) -> int:
    if hasattr(os, 'pidfd_open'):
        return os.pidfd_open(pid, 0)
    # Some supported Python builds omit pidfd wrappers despite a capable libc.
    library = ctypes.CDLL(None, use_errno=True)
    function = library.pidfd_open
    function.argtypes = (ctypes.c_int, ctypes.c_uint)
    function.restype = ctypes.c_int
    descriptor = function(pid, 0)
    if descriptor < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return descriptor


def _pidfd_signal(descriptor: int, sig: int) -> None:
    if hasattr(signal, 'pidfd_send_signal'):
        signal.pidfd_send_signal(descriptor, sig)
        return
    library = ctypes.CDLL(None, use_errno=True)
    function = library.pidfd_send_signal
    function.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint)
    function.restype = ctypes.c_int
    if function(descriptor, sig, None, 0) < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def stop_processes(identities: dict[int, str], *, include: int | None = None) -> bool:
    targets = dict(identities)
    if include is not None:
        info = _process(include)
        if info:
            targets[include] = info[1]
    def alive():
        result = []
        for pid, started in targets.items():
            info = _process(pid)
            if info and info[1] == started and info[2] != 'Z' and pid != os.getpid():
                result.append(pid)
        return result
    for sig, grace in ((signal.SIGTERM, 0.08), (signal.SIGKILL, 0.15)):
        for pid in alive():
            descriptor = None
            try:
                # Hold a kernel process identity across validation and signal;
                # a recycled PID must never refer to an unrelated process.
                descriptor = _pidfd_open(pid)
                current = _process(pid)
                if current and current[1] == targets[pid]:
                    _pidfd_signal(descriptor, sig)
            except ProcessLookupError:
                pass
            except (PermissionError, AttributeError, OSError):
                return False
            finally:
                if descriptor is not None:
                    os.close(descriptor)
        deadline = time.monotonic() + grace
        while alive() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not alive():
            return True
    return not alive()


def _cleanup_descendants() -> bool:
    # The worker is a subreaper: detached grandchildren remain its children.
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline:
        children = process_tree(os.getpid())
        if not stop_processes(children):
            return False
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if not pid:
                    break
            except ChildProcessError:
                break
        if not process_tree(os.getpid()):
            return True
    return False


def worker_entry(target: str, request: dict, sender, cancelled) -> None:
    """All worker events carry the immutable admitted identity and attempt."""
    lock = threading.Lock()
    def emit(event):
        envelope = {**event, 'run_id': request['run_id'], 'attempt': request['attempt']}
        with lock:
            sender.send(envelope)
    result = {'status': 'failed', 'error': 'Worker did not start.', 'reply': ''}
    try:
        os.setsid()
        # PR_SET_CHILD_SUBREAPER. Fail closed on platforms without Linux ownership.
        if ctypes.CDLL(None, use_errno=True).prctl(36, 1, 0, 0, 0) != 0:
            raise RuntimeError('Could not establish worker descendant ownership.')
        os.environ['TALECLAW_RUN_ID'] = request['run_id']
        module, name = target.split(':', 1)
        function = getattr(importlib.import_module(module), name)
        result = function(request, emit, cancelled)
        if not isinstance(result, dict):
            raise TypeError('Worker target must return a result mapping.')
    except BaseException as exc:
        result = {'status': 'failed', 'error': f'{type(exc).__name__}: {exc}', 'reply': ''}
    finally:
        try:
            result['cleanup_confirmed'] = _cleanup_descendants()
        except BaseException as cleanup_error:
            result['cleanup_confirmed'] = False
            result['cleanup_error'] = f'{type(cleanup_error).__name__}: {cleanup_error}'
        try:
            emit({'type': '_result', 'result': result})
        finally:
            sender.close()


def _attachments(request, emit):
    relative_paths = request.get('attachment_paths') or []
    if not relative_paths:
        return request.get('attachments'), request.get('display_content')
    from web.mineru import MinerUClient, build_attachment_payloads, display_message
    root = storage_root_for_user(Path(__file__).resolve().parents[1], request['user_id']).resolve()
    paths = []
    for relative in relative_paths:
        raw = Path(unquote(relative).strip().lstrip('/'))
        if raw.is_absolute() or '..' in raw.parts:
            raise ValueError('Attachment path escapes user storage.')
        path = root / raw
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Attachment does not exist or escapes user storage.')
        paths.append(path)
    emit({'type': 'status', 'status': 'mineru', 'text': '正在使用 MinerU 精准解析附件…'})
    with MinerUClient() as client:
        parsed = [client.parse_file(path) for path in paths]
    emit({'type': 'status', 'status': 'agent', 'text': '附件解析完成，正在交给 LLM…'})
    return build_attachment_payloads(parsed, paths), display_message(request['content'], parsed)


def _save_fallback_title(runtime, storage_id):
    """No post-run writer: preserve existing titles, derive new ones locally."""
    from web.session_titles import first_question_answer, normalize_session_title
    sessions = runtime.services.session_manager
    session = sessions.get_or_create(storage_id)
    if str(session.metadata.get('title') or '').strip():
        return
    pair = first_question_answer(session.messages)
    if pair:
        session.metadata['title'] = normalize_session_title(pair[0]) or '新会话'
        sessions.save(session)


def run_agent(request, emit, cancelled):
    """Production target: one fresh runtime, provider pool and session cache/run."""
    from applications.bootstrap import build_runtime
    from runtime.trace.trace_store import TraceStore

    class WorkerTraceStore(TraceStore):
        def write_run_state(self, state):
            if state.run_id == request['run_id']:
                emit({'type': '_state', 'state': state.to_dict()})
            else:
                super().write_run_state(state)

    async def execute():
        if cancelled.is_set():
            return {'status': 'stopped', 'reply': ''}
        attachments, display_content = _attachments(request, emit)
        if cancelled.is_set():
            return {'status': 'stopped', 'reply': ''}
        runtime = build_runtime()
        original = runtime.coordinator.trace_store
        proxy = WorkerTraceStore.__new__(WorkerTraceStore)
        proxy.__dict__.update(original.__dict__)
        proxy.root = Path(request['run_root'])
        runtime.coordinator.trace_store = proxy
        runtime.services = replace(runtime.services, trace_store=proxy)
        storage_id = web_session_id(request['user_id'], request['session_id'])
        unsubscribe = proxy.subscribe(storage_id, lambda event: emit({'type': 'event', 'data': event}))
        finished = threading.Event()
        def relay_cancel():
            while not finished.wait(0.02):
                if cancelled.is_set():
                    runtime.request_cancel(storage_id)
        watcher = threading.Thread(target=relay_cancel, name='worker-cancellation', daemon=True)
        watcher.start()
        reply = []
        async def outbound(message):
            if message.chat_id == web_chat_id(request['user_id'], request['session_id']):
                reply.append(message.content)
        runtime.bus.subscribe_outbound('web', outbound)
        runtime.start()
        def on_text(text):
            emit({'type': 'delta', 'text': text})
        on_text.on_thinking = lambda text: emit({'type': 'thinking', 'text': text})
        on_text.on_assistant_segment = lambda data: emit({'type': 'assistant_segment', 'data': data})
        on_text.on_assistant_completed = lambda data: emit({'type': 'assistant_completed', 'data': data})
        metadata = {key: request[key] for key in ('user_id', 'user_role', 'thinking_enabled', 'model_profile')}
        metadata.update(run_id=request['run_id'], _supervised_run=True,
                        coding_default_workspace_mode='worktree')
        for key in ('workspace_root', 'coding_task'):
            if request.get(key) is not None:
                metadata[key] = request[key]
        if attachments:
            metadata['attachments'] = attachments
        if display_content:
            metadata['display_content'] = display_content
        try:
            state = await runtime.run_message(content=request['content'], channel='web',
                                              chat_id=web_chat_id(request['user_id'], request['session_id']),
                                              metadata=metadata, on_text=on_text)
            await asyncio.sleep(0)  # Dispatch the already-produced outbound response.
            if state.run_id != request['run_id']:
                raise RuntimeError('Runtime changed the admitted run identity.')
            try:
                _save_fallback_title(runtime, storage_id)
            except Exception as exc:
                # The reply is already durable; title projection is optional.
                emit({'type': 'event', 'data': {'event': 'session_title.failed',
                                               'run_id': request['run_id'],
                                               'payload': {'error_type': type(exc).__name__}}})
            return {'status': state.status, 'reply': state.final_answer or (reply[-1] if reply else ''),
                    'error': state.error}
        finally:
            finished.set()
            watcher.join(timeout=0.1)
            unsubscribe()
            await runtime.stop()
    return asyncio.run(execute())
