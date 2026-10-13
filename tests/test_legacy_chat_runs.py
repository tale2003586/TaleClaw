"""Legacy HTTP routes use the same durable run protocol as the new UI."""
from http import HTTPStatus

import pytest
from types import SimpleNamespace
from unittest.mock import patch

from web.agent_service import RunConflictError, RunNotFoundError
from web.server import RequestHandler


class Service:
    def __init__(self, *, status='completed', events=None):
        self.run = dict(run_id='run_accepted', session_id='a', status=status, last_seq=1)
        self.events = events if events is not None else [dict(type='complete', seq=1, run_id='run_accepted', session_id='a', reply='answer')]
        self.submissions = []
        self.cancellations = []
    def submit(self, **kwargs):
        self.submissions.append(kwargs)
        return dict(self.run)
    def get_run(self, run_id, *, user_id):
        assert run_id == 'run_accepted' and user_id == 'alice'
        return dict(self.run)
    def events_after(self, run_id, *, user_id, after=0, wait_seconds=0):
        assert run_id == 'run_accepted' and user_id == 'alice'
        return [dict(event) for event in self.events if event['seq'] > after]
    def cancel_run(self, run_id, *, user_id):
        self.cancellations.append((run_id, user_id))
        return {**self.run, 'status': 'stopped'}


def handler(payload, service=None):
    value = object.__new__(RequestHandler)
    value.agent_service = service or Service()
    value.headers = {"Content-Type": "application/json", "Host": "localhost:8000"}
    value._current_user = lambda: SimpleNamespace(user_id='alice', role='user')
    value._read_json_body = lambda: payload
    value.responses = []
    value._send_json = lambda body, status=HTTPStatus.OK, **kwargs: value.responses.append((body, status))
    value._send_stream_headers = lambda: None
    value.stream = []
    value._send_stream_event = value.stream.append
    return value


def test_legacy_sync_propagates_idempotency_and_returns_accepted_run_identity():
    value = handler({'session_id': 'a', 'message': '/coding', 'request_id': 'same-request'})
    with patch('web.server.read_session', return_value={'id': 'web:alice:a'}):
        value._handle_chat()
    assert value.agent_service.submissions[0]['request_id'] == 'same-request'
    assert value.agent_service.submissions[0]['content'] == '/coding'
    body, status = value.responses[-1]
    assert status == HTTPStatus.OK
    assert body['run_id'] == 'run_accepted' and body['reply'] == 'answer'


def test_legacy_sync_timeout_returns_pending_run_without_resubmission():
    value = handler({'session_id': 'a', 'message': 'long task'}, Service(status='running', events=[]))
    with patch('time.monotonic', side_effect=[0, 9999]):
        value._handle_chat()
    body, status = value.responses[-1]
    assert status == HTTPStatus.ACCEPTED
    assert body['run_id'] == 'run_accepted' and body['pending'] is True
    assert len(value.agent_service.submissions) == 1
    assert value.agent_service.cancellations == []


def test_legacy_stream_uses_run_replay_and_disconnect_does_not_cancel():
    value = handler({'session_id': 'a', 'message': 'hello', 'request_id': 'stable'})
    def disconnected(event):
        value.stream.append(event)
        raise BrokenPipeError
    value._send_stream_event = disconnected
    with patch('web.server.read_session', return_value=None):
        value._handle_chat_stream()
    assert value.agent_service.submissions[0]['request_id'] == 'stable'
    assert value.stream[0]['run_id'] == 'run_accepted'
    assert value.agent_service.cancellations == []


def test_legacy_stream_passes_attachment_paths_without_parsing_in_http_handler():
    from pathlib import Path
    value = handler({'session_id': 'a', 'message': 'read report', 'attachments': ['report.pdf']})
    with patch('web.server._safe_storage_path', return_value=Path(__file__)), \
         patch('web.mineru.MinerUClient', side_effect=AssertionError('HTTP must not parse attachments')), \
         patch('web.server.read_session', return_value=None):
        value._handle_chat_stream()
    assert value.agent_service.submissions[0]['attachment_paths'] == ['report.pdf']
    assert 'attachments' not in value.agent_service.submissions[0]


def test_legacy_stop_requires_exact_run_id_and_does_not_stop_current_session():
    value = handler({'session_id': 'a'})
    value._handle_chat_stop()
    body, status = value.responses[-1]
    assert status == HTTPStatus.BAD_REQUEST
    assert 'run_id' in body['error']
    assert value.agent_service.cancellations == []


def test_legacy_stop_authorizes_the_specific_run():
    value = handler({'run_id': 'run_accepted'})
    value._handle_chat_stop()
    body, status = value.responses[-1]
    assert status == HTTPStatus.OK and body['run_id'] == 'run_accepted'
    assert value.agent_service.cancellations == [('run_accepted', 'alice')]


def test_legacy_run_conflict_is_409_not_generic_service_failure():
    value = handler({'session_id': 'a', 'message': 'hello'})
    def busy(**kwargs):
        raise RunConflictError('active run')
    value.agent_service.submit = busy
    value._handle_chat()
    assert value.responses[-1][1] == HTTPStatus.CONFLICT


def test_legacy_stop_does_not_reveal_another_users_run():
    value = handler({'run_id': 'run_private'})
    def denied(*args, **kwargs):
        raise RunNotFoundError('Run not found')
    value.agent_service.cancel_run = denied
    value._handle_chat_stop()
    assert value.responses[-1][1] == HTTPStatus.NOT_FOUND


@pytest.mark.parametrize("method", ["_handle_chat", "_handle_chat_stream", "_handle_chat_stop"])
@pytest.mark.parametrize("headers,status", [
    ({"Origin": "https://attacker.invalid"}, HTTPStatus.FORBIDDEN),
    ({"Content-Type": "text/plain"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
])
def test_legacy_mutation_rejects_cross_origin_or_non_json_before_reading_body(method, headers, status):
    value = handler({"session_id": "a", "message": "work", "run_id": "run_accepted"})
    value.headers.update(headers)
    def forbidden_read():
        raise AssertionError("Rejected requests must not be parsed or admitted")
    value._read_json_body = forbidden_read
    getattr(value, method)()
    assert value.responses[-1][1] == status
    assert value.agent_service.submissions == []
    assert value.agent_service.cancellations == []
