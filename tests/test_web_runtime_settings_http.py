import io
import json
from http import HTTPStatus
from unittest.mock import Mock, patch

import pytest

from web.auth_store import AuthenticatedUser
from web.server import RequestHandler, _configured_model_options


def request(path, *, method="GET", payload=None, role="admin", headers=None, authorized=True):
    handler = object.__new__(RequestHandler)
    handler.path = path
    body = json.dumps(payload).encode() if payload is not None else b""
    handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                       "Host": "localhost:8000", **(headers or {})}
    handler.rfile = io.BytesIO(body)
    handler.auth_user = AuthenticatedUser(user_id="alice", role=role)
    handler._authorize = Mock(return_value=authorized)
    handler.agent_service = Mock()
    responses = []
    handler._send_json = lambda value, status=HTTPStatus.OK, headers=None: responses.append(
        (value, status, headers or {}))
    getattr(handler, "do_" + method)()
    handler.agent_service.ensure_started.assert_not_called()
    return responses


def test_model_settings_are_admin_only_and_do_not_start_the_runtime():
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        store.return_value.get_public.return_value = {"profiles": [], "revision": 0}
        assert request("/api/settings/models")[0] == (
            {"profiles": [], "revision": 0}, HTTPStatus.OK, {"Cache-Control": "no-store"})
        assert request("/api/settings/models", role="user")[0][1] == HTTPStatus.FORBIDDEN
        assert request("/api/settings/models", method="POST", role="user", payload={})[0][1] == HTTPStatus.FORBIDDEN
        assert request("/api/settings/models", authorized=False) == []
        store.return_value.update.assert_not_called()


@pytest.mark.parametrize("headers,status", [
    ({"Content-Type": "text/plain"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
    ({"Origin": "https://other.example"}, HTTPStatus.FORBIDDEN),
    ({"Origin": "null"}, HTTPStatus.FORBIDDEN),
    ({"Origin": "http://["}, HTTPStatus.FORBIDDEN),
    ({"Sec-Fetch-Site": "cross-site"}, HTTPStatus.FORBIDDEN),
    ({"Content-Length": "-1"}, HTTPStatus.BAD_REQUEST),
    ({"Content-Length": str(1024 * 1024 + 1)}, HTTPStatus.BAD_REQUEST),
])
def test_model_writes_reject_cross_origin_forms_and_oversized_requests(headers, status):
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        assert request("/api/settings/models", method="POST", payload={}, headers=headers)[0][1] == status
        store.return_value.update.assert_not_called()


def test_model_updates_map_conflicts_and_storage_errors_without_exposing_credentials():
    from web.runtime_settings import SettingsConflictError, SettingsStorageError, SettingsValidationError
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        for error, status in [(SettingsConflictError("Changed"), HTTPStatus.CONFLICT),
                              (SettingsValidationError("Invalid"), HTTPStatus.BAD_REQUEST),
                              (SettingsStorageError("secret-key-and-path"), HTTPStatus.SERVICE_UNAVAILABLE)]:
            store.return_value.update.side_effect = error
            response = request("/api/settings/models", method="POST", payload={"revision": 1})[0]
            assert response[1] == status
            assert "secret-key-and-path" not in json.dumps(response[0])
        store.return_value.update.side_effect = None
        store.return_value.update.return_value = {"revision": 2, "profiles": []}
        response = request("/api/settings/models", method="POST", payload={"revision": 1},
                           headers={"Origin": "http://localhost:8000"})[0]
        assert response[1] == HTTPStatus.OK
        assert response[2]["Cache-Control"] == "no-store"


def test_health_and_profile_validation_use_effective_model_configuration():
    env = {"LLM_PROVIDER": "relay", "RELAY_API_KEY": "fake-key", "RELAY_MODEL": "hot-model",
           "RELAY_BASE_URL": "https://relay.example/v1"}
    with patch("web.runtime_settings.effective_environ", return_value=env):
        options, default = _configured_model_options()
    assert default == "relay"
    assert options == [{"profile": "relay", "provider": "relay", "model": "hot-model",
                        "supports_thinking": False}]


def test_invalid_inherited_chat_route_keeps_console_accessible_for_repair():
    env = {"LLM_PROVIDER": "relay", "RELAY_API_KEY": "fake-key", "RELAY_MODEL": "hot-model",
           "RELAY_BASE_URL": "https://relay.example/v1", "LLM_ROUTE_CHAT": "unknown"}
    with patch("web.runtime_settings.effective_environ", return_value=env):
        assert _configured_model_options() == ([], "")


def test_memory_reads_default_to_current_user_and_keep_legacy_separate():
    with patch("web.memory_view.read_memory_overview", return_value={"items": []}) as read, \
         patch("web.server.read_memory_files", return_value=[]) as legacy:
        response = request("/api/memory", role="user")[0]
        read.assert_called_once_with("alice", session=None, limit=100, offset=0)
        legacy.assert_not_called()
        assert response == ({"items": []}, HTTPStatus.OK, {"Cache-Control": "no-store"})
        assert request("/api/memory/legacy", role="user")[0][0] == {"files": []}
        legacy.assert_called_once_with("alice")


def test_memory_context_comes_only_from_an_authenticated_server_session():
    session = {"id": "web:alice:task", "metadata": {"task_id": "task-alice"}}
    with patch("web.memory_view.read_memory_overview", return_value={"items": []}) as read, \
         patch("web.memory_view.load_memory_session", return_value=session) as load:
        assert request("/api/memory?session_id=task&limit=20&offset=40", role="user")[0][1] == HTTPStatus.OK
        load.assert_called_once_with("task", "alice")
        read.assert_called_once_with("alice", session=session, limit=20, offset=40)


@pytest.mark.parametrize("query", [
    "owner_id=bob", "owner_scope=task", "session_id=x&session_id=y", "limit=-1", "limit=201",
    "limit=x", "offset=-1", "offset=x", "session_id=web:bob:private",
])
def test_memory_rejects_arbitrary_scopes_invalid_pagination_and_foreign_sessions(query):
    with patch("web.memory_view.read_memory_overview") as read:
        assert request("/api/memory?" + query, role="user")[0][1] == HTTPStatus.BAD_REQUEST
        read.assert_not_called()


def test_memory_authorization_stops_before_any_storage_read():
    with patch("web.memory_view.read_memory_overview") as read:
        assert request("/api/memory", authorized=False) == []
        read.assert_not_called()
