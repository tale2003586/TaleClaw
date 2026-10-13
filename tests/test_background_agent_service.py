from pathlib import Path
import time

import pytest

from web.agent_service import (
    AgentService, RunConflictError, RunNotFoundError, RunServiceUnavailableError,
)


def service(tmp_path, **kwargs):
    return AgentService(root=tmp_path / 'runs', worker_target='tests.run_worker_fakes:run_fake',
                        cancel_grace_seconds=0.5, **kwargs)


def submit(app, session='a', content='hello', request='req', user='alice'):
    return app.submit(session_id=session, content=content, user_id=user,
                      user_role='user', request_id=request)


def until(app, run, statuses, seconds=6):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = app.get_run(run['run_id'], user_id='alice')
        if value['status'] in statuses:
            return value
        time.sleep(0.015)
    raise AssertionError(value)


def test_real_process_parallelism_and_capacity(tmp_path):
    app = service(tmp_path, max_workers=2)
    try:
        release = tmp_path / 'release'
        runs = [submit(app, str(i), 'wait:' + str(release), str(i)) for i in range(3)]
        for run in runs[:2]:
            until(app, run, {'running'})
        assert app.get_run(runs[2]['run_id'], user_id='alice')['status'] == 'queued'
        for run in runs[:2]:
            events = app.events_after(run['run_id'], user_id='alice', after=0, wait_seconds=1)
            assert events
        release.touch()
        for run in runs:
            assert until(app, run, {'completed'})['reply'].startswith('reply:wait:')
    finally:
        app.stop()


def test_single_session_admission_and_durable_idempotency(tmp_path):
    app = service(tmp_path)
    release = tmp_path / 'release'
    content = 'wait:' + str(release)
    try:
        first = submit(app, content=content)
        assert submit(app, content=content)['run_id'] == first['run_id']
        with pytest.raises(RunConflictError):
            submit(app, content='different')
        with pytest.raises(RunConflictError):
            submit(app, content=content, request='second')
        release.touch()
        until(app, first, {'completed'})
    finally:
        app.stop()
    reopened = service(tmp_path)
    try:
        assert submit(reopened, content=content)['run_id'] == first['run_id']
    finally:
        reopened.stop()


def test_authorized_event_replay_and_disconnect_does_not_cancel(tmp_path):
    app = service(tmp_path)
    try:
        run = submit(app)
        with pytest.raises(RunNotFoundError):
            app.get_run(run['run_id'], user_id='bob')
        with pytest.raises(RunNotFoundError):
            app.events_after(run['run_id'], user_id='bob')
        with pytest.raises(RunNotFoundError):
            app.cancel_run(run['run_id'], user_id='bob')
        assert app.list_runs(user_id='bob') == []
        final = until(app, run, {'completed'})
        events = app.events_after(run['run_id'], user_id='alice')
        assert [e['seq'] for e in events] == list(range(1, final['last_seq'] + 1))
        assert events[-1]['type'] == 'complete'
        assert any(e['type'] == 'thinking' for e in events)
        assert app.events_after(run['run_id'], user_id='alice', after=events[1]['seq']) == events[2:]
        assert all(e['run_id'] == run['run_id'] and e['session_id'] == 'a' for e in events)
    finally:
        app.stop()


def test_cancel_queued_and_running_then_allow_next_turn(tmp_path):
    app = service(tmp_path, max_workers=1)
    try:
        active = submit(app, content='wait:' + str(tmp_path / 'release'))
        until(app, active, {'running'})
        queued = submit(app, session='b', request='b')
        assert app.cancel_run(queued['run_id'], user_id='alice')['status'] == 'stopped'
        app.cancel_run(active['run_id'], user_id='alice')
        assert until(app, active, {'stopped'})['status'] == 'stopped'
        next_run = submit(app, request='next')
        until(app, next_run, {'completed'})
    finally:
        app.stop()


def test_forced_cancel_quarantines_and_blocks_same_session(tmp_path):
    app = service(tmp_path)
    try:
        run = submit(app, content='ignore-cancel')
        until(app, run, {'running'})
        # Wait until target is actually executing, not merely spawned.
        deadline = time.monotonic() + 5
        while not any(e['type'] == 'delta' for e in app.events_after(run['run_id'], user_id='alice')):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        app.cancel_run(run['run_id'], user_id='alice')
        final = until(app, run, {'interrupted'})
        assert final['quarantined'] is True
        with pytest.raises(RunConflictError):
            submit(app, request='new')
    finally:
        app.stop()


def test_crashed_worker_cannot_release_ownership_as_success(tmp_path):
    app = service(tmp_path)
    marker = tmp_path / 'orphan'
    try:
        run = submit(app, content='crash:' + str(marker))
        final = until(app, run, {'interrupted'})
        assert final['quarantined']
        time.sleep(0.9)
        assert not marker.exists(), 'tracked descendant survived worker crash'
    finally:
        app.stop()


def test_exclusive_supervisor_lock(tmp_path):
    first, second = service(tmp_path), service(tmp_path)
    try:
        first.ensure_started()
        with pytest.raises(RunServiceUnavailableError):
            second.ensure_started()
    finally:
        first.stop()
        second.stop()


def test_legacy_ask_uses_same_persistent_run_and_delete_refuses_active(tmp_path):
    deleted = []
    app = service(tmp_path, session_deleter=lambda key: deleted.append(key) or True)
    try:
        emitted = []
        assert app.ask_stream(session_id='a', content='hello', user_id='alice',
                              user_role='user', on_text=emitted.append) == 'reply:hello'
        assert emitted == ['hello']
        assert len(app.list_runs(user_id='alice')) == 1
        run = submit(app, session='b', content='wait:' + str(tmp_path / 'release'), request='b')
        with pytest.raises(RunConflictError):
            app.delete_session('web:alice:b')
        assert app.delete_session('web:alice:a')
        assert deleted == ['web:alice:a']
        app.cancel_run(run['run_id'], user_id='alice')
    finally:
        app.stop()


def test_restart_interrupts_unclaimed_run_without_automatic_reexecution(tmp_path, monkeypatch):
    app = service(tmp_path)
    monkeypatch.setattr(app, '_launch', lambda state: None)
    run = submit(app)
    # Simulate supervisor loss after durable acceptance, before dispatch. No
    # child exists in this fixture; leave the persisted queued state untouched.
    with app._condition:
        app._stopping = True
        app._condition.notify_all()
    app._thread.join(timeout=2)
    assert not app._thread.is_alive()
    reopened = service(tmp_path)
    try:
        restored = reopened.get_run(run['run_id'], user_id='alice')
        assert restored['status'] == 'interrupted'
        assert restored['quarantined']
        assert not reopened._workers
        assert submit(reopened)['run_id'] == run['run_id']
        with pytest.raises(RunConflictError):
            submit(reopened, request='different')
    finally:
        reopened.stop()


def test_event_journal_repairs_partial_tail_and_recovers_committed_transition(tmp_path):
    import json
    app = service(tmp_path)
    try:
        run = submit(app)
        final = until(app, run, {'completed'})
    finally:
        app.stop()
    directory = tmp_path / 'runs' / run['run_id']
    # A crash after journal fsync and before replacing the snapshot is repaired
    # from the journal, without repeating the completed execution.
    snapshot = json.loads((directory / 'run_state.json').read_text())
    snapshot['status'] = 'running'
    (directory / 'run_state.json').write_text(json.dumps(snapshot))
    with (directory / 'stream.jsonl').open('ab') as handle:
        handle.write(b'{"event":')
    reopened = service(tmp_path)
    try:
        restored = reopened.get_run(run['run_id'], user_id='alice')
        assert restored['status'] == 'completed'
        assert restored['last_seq'] == final['last_seq']
        assert (directory / 'stream.jsonl').read_bytes().endswith(b'\n')
    finally:
        reopened.stop()


def test_wait_timeout_keeps_background_run_alive(tmp_path):
    app = service(tmp_path)
    release = tmp_path / 'release'
    try:
        with pytest.raises(TimeoutError):
            app.ask_stream(session_id='a', content='wait:' + str(release), user_id='alice',
                           timeout=0.03)
        run = app.list_runs(user_id='alice')[0]
        assert run['status'] in {'queued', 'running'}
        release.touch()
        until(app, run, {'completed'})
    finally:
        app.stop()


def test_same_request_id_is_scoped_to_user(tmp_path):
    app = service(tmp_path)
    try:
        one = submit(app)
        two = submit(app, user='bob')
        assert one['run_id'] != two['run_id']
        with pytest.raises(ValueError):
            app.events_after(one['run_id'], user_id='alice', after=10_000)
    finally:
        app.stop()


def test_production_worker_adapter_preserves_identity_and_frozen_services(tmp_path, monkeypatch):
    """Exercise the real worker adapter with an entirely offline runtime factory."""
    import asyncio
    from dataclasses import dataclass
    import sys
    from types import ModuleType, SimpleNamespace
    import threading
    from runtime.trace.run_state import RunState
    from web import run_worker

    class Trace:
        def __init__(self):
            self.root = tmp_path
        def subscribe(self, session, callback):
            return lambda: None
        def write_run_state(self, state):
            raise AssertionError('root RunState must be written by supervisor only')

    @dataclass(frozen=True)
    class Services:
        trace_store: object
        session_manager: object = None

    closed = []
    captured = []
    class Runtime:
        def __init__(self):
            self.coordinator = SimpleNamespace(trace_store=Trace())
            self.services = Services(self.coordinator.trace_store)
            self.bus = SimpleNamespace(subscribe_outbound=lambda *args: None)
        def start(self):
            pass
        async def stop(self):
            closed.append(True)
        def request_cancel(self, session_id):
            pass
        async def run_message(self, *, content, channel, chat_id, metadata, on_text):
            captured.append(metadata)
            state = RunState.create(run_id=metadata['run_id'], session_id='web:alice:a')
            self.coordinator.trace_store.write_run_state(state)
            on_text('answer')
            on_text.on_thinking('thought')
            state.finish_success('answer')
            return state

    bootstrap = ModuleType('applications.bootstrap')
    bootstrap.build_runtime = Runtime
    trace_module = ModuleType('runtime.trace.trace_store')
    trace_module.TraceStore = Trace
    monkeypatch.setitem(sys.modules, 'applications.bootstrap', bootstrap)
    monkeypatch.setitem(sys.modules, 'runtime.trace.trace_store', trace_module)
    monkeypatch.setattr(run_worker, '_save_fallback_title', lambda *args: None)
    request = dict(run_id='run-admitted', run_root=str(tmp_path), attempt=1, session_id='a',
                   content='question', user_id='alice', user_role='user', thinking_enabled=False,
                   model_profile='')
    events = []
    result = run_worker.run_agent(request, events.append, threading.Event())
    assert captured[0]['run_id'] == 'run-admitted'
    assert captured[0]['_supervised_run'] is True
    assert result == {'status': 'completed', 'reply': 'answer', 'error': None}
    assert closed == [True]
    assert [event['type'] for event in events] == ['_state', 'delta', 'thinking']


def test_failed_durable_acceptance_never_dispatches(tmp_path, monkeypatch):
    from web import agent_service
    app = service(tmp_path)
    original = agent_service._atomic
    def fail_snapshot(path, value):
        if path.name == 'run_state.json':
            raise OSError('simulated snapshot write failure')
        return original(path, value)
    monkeypatch.setattr(agent_service, '_atomic', fail_snapshot)
    try:
        with pytest.raises(OSError):
            submit(app)
        time.sleep(0.06)
        assert not app._workers
        with pytest.raises(RunServiceUnavailableError):
            submit(app, request='new')
    finally:
        # Stop must also work when the service failed before starting a worker.
        monkeypatch.setattr(agent_service, '_atomic', original)
        app.stop()


def test_managed_workspace_dto_uses_binding_not_path(tmp_path):
    from runtime.trace.run_state import RunState
    app = service(tmp_path)
    state = RunState.create(session_id='web:alice:a', user_id='alice')
    state.metadata.update(background_execution=dict(client_session_id='a', created_at='now',
                                                   updated_at='now', last_seq=0),
                          worktree_binding={'worktree_id': 'wt-one', 'workspace_root': '/managed/one',
                                            'task_id': 'coding-one', 'service_root': '/managed'})
    dto = app._dto(state)
    assert dto['workspace'] == state.metadata['worktree_binding']
    assert dto['workspace_root'] == '/managed/one'
    assert 'worktree' not in dto


def test_normal_worker_exit_cleans_detached_children_before_completion(tmp_path):
    app = service(tmp_path)
    marker = tmp_path / 'background-write'
    try:
        run = submit(app, content='background:' + str(marker))
        assert until(app, run, {'completed'})['quarantined'] is False
        time.sleep(0.85)
        assert not marker.exists()
    finally:
        app.stop()


def test_quarantined_in_place_workspace_blocks_other_session_and_nested_root(tmp_path):
    app = service(tmp_path)
    workspace = tmp_path / 'checkout'
    workspace.mkdir()
    nested = workspace / 'nested'
    nested.mkdir()
    try:
        run = app.submit(session_id='a', content='ignore-cancel', user_id='alice', user_role='user',
                         request_id='first', workspace_root=str(workspace))
        until(app, run, {'running'})
        app.cancel_run(run['run_id'], user_id='alice')
        until(app, run, {'interrupted'})
        for path in (workspace, nested):
            with pytest.raises(RunConflictError):
                app.submit(session_id='other', content='hello', user_id='alice', user_role='user',
                           request_id='other', workspace_root=str(path))
        separate = tmp_path / 'separate'
        separate.mkdir()
        other = app.submit(session_id='other', content='hello', user_id='alice', user_role='user',
                           request_id='other', workspace_root=str(separate))
        until(app, other, {'completed'})
    finally:
        app.stop()
