"""Run identity boundary; collected in the guarded isolated application suite."""
from types import SimpleNamespace

from applications.turn_coordinator import TurnCoordinator


def _start(metadata):
    coordinator = object.__new__(TurnCoordinator)
    coordinator.trace_store = None
    session = SimpleNamespace(id='web:alice:a', active_agent='coding', metadata={'user_id': 'alice', 'user_role': 'user'})
    inbound = SimpleNamespace(metadata=metadata, channel='web', chat_id='alice:a', sender='user', media=[])
    return coordinator._start_run(inbound, session)


def test_internal_worker_preserves_durably_admitted_run_id():
    assert _start({'run_id': 'run_admitted', '_supervised_run': True}).run_id == 'run_admitted'


def test_plain_inbound_metadata_cannot_choose_run_identity():
    assert _start({'run_id': 'run_untrusted'}).run_id != 'run_untrusted'


def test_only_trusted_web_worker_sets_default_workspace_policy():
    coordinator = object.__new__(TurnCoordinator)
    session = SimpleNamespace(metadata={})
    coordinator._apply_inbound_identity(session, SimpleNamespace(metadata={'coding_default_workspace_mode': 'worktree'}))
    assert 'coding_default_workspace_mode' not in session.metadata
    coordinator._apply_inbound_identity(session, SimpleNamespace(metadata={'_supervised_run': True, 'coding_default_workspace_mode': 'worktree'}))
    assert session.metadata['coding_default_workspace_mode'] == 'worktree'
