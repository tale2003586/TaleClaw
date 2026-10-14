from http import HTTPStatus
import io
import json
from unittest.mock import patch

import pytest

from tests.test_web_runtime_settings_http import request
from web.server import RequestHandler


@pytest.mark.parametrize("operation", ["probe", "preview"])
def test_model_workbench_requires_admin_and_same_origin_before_any_operation(operation):
    path = "/api/settings/models/" + operation
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        assert request(path, method="POST", payload={}, role="user")[0][1] == HTTPStatus.FORBIDDEN
        assert request(path, method="POST", payload={}, authorized=False) == []
        for headers, status in [
            ({"Origin": "https://other.example"}, HTTPStatus.FORBIDDEN),
            ({"Origin": "null"}, HTTPStatus.FORBIDDEN),
            ({"Sec-Fetch-Site": "cross-site"}, HTTPStatus.FORBIDDEN),
            ({"Content-Type": "text/plain"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
        ]:
            assert request(path, method="POST", payload={}, headers=headers)[0][1] == status
        store.assert_not_called()


def test_probe_dispatch_never_saves_or_bootstraps_and_returns_private_no_store_response():
    with patch("web.runtime_settings.RuntimeSettingsStore") as store, \
         patch("web.model_diagnostics.probe_models", return_value={"status": "ok", "models": ["test-model"]}) as probe:
        payload = {"revision": 1, "profile": {"name": "relay"}}
        response = request("/api/settings/models/probe", method="POST", payload=payload)[0]
        assert response == ({"status": "ok", "models": ["test-model"]}, HTTPStatus.OK,
                            {"Cache-Control": "no-store"})
        probe.assert_called_once_with(store.return_value, payload)
        store.return_value.update.assert_not_called()
        assert request("/api/settings/models/probe", method="POST", payload=payload,
                       headers={"Content-Length": str(64 * 1024 + 1)})[0][1] == HTTPStatus.BAD_REQUEST
        assert probe.call_count == 1


def test_preview_dispatch_never_saves_and_preserves_conflict_semantics():
    from web.runtime_settings import SettingsConflictError
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        store.return_value.preview.return_value = {"configured": True, "routes": []}
        payload = {"revision": 1}
        response = request("/api/settings/models/preview", method="POST", payload=payload)[0]
        assert response[1] == HTTPStatus.OK and response[2]["Cache-Control"] == "no-store"
        store.return_value.preview.assert_called_once_with(payload)
        store.return_value.update.assert_not_called()
        store.return_value.preview.side_effect = SettingsConflictError("changed")
        assert request("/api/settings/models/preview", method="POST", payload=payload)[0][1] == HTTPStatus.CONFLICT


def test_global_memory_filters_are_forwarded_before_storage_pagination():
    with patch("web.memory_view.read_memory_overview", return_value={"items": [], "filtered_total": 0}) as read:
        response = request("/api/memory?query=201&status=candidate&kind=fact&limit=10&offset=20", role="user")[0]
        assert response[1] == HTTPStatus.OK
        read.assert_called_once_with("alice", session=None, limit=10, offset=20,
                                     query="201", status="candidate", kind="fact")
        assert request("/api/memory?query=x&query=y", role="user")[0][1] == HTTPStatus.BAD_REQUEST
        assert read.call_count == 1


def test_memory_actions_authorize_current_user_and_require_same_origin_with_bounded_body():
    with patch("web.memory_management.apply_memory_action", return_value={"item": {}, "index_sync": "queued"}) as act:
        payload = {"id": "personal", "expected_version": 1, "action": "confirm"}
        response = request("/api/memory/action", method="POST", payload=payload, role="user")[0]
        assert response[1] == HTTPStatus.OK and response[2]["Cache-Control"] == "no-store"
        act.assert_called_once_with("alice", payload)
        assert request("/api/memory/action", method="POST", payload=payload, authorized=False) == []
        for headers, status in [
            ({"Origin": "https://other.example"}, HTTPStatus.FORBIDDEN),
            ({"Origin": "null"}, HTTPStatus.FORBIDDEN),
            ({"Sec-Fetch-Site": "cross-site"}, HTTPStatus.FORBIDDEN),
            ({"Content-Type": "text/plain"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE),
            ({"Content-Length": "-1"}, HTTPStatus.BAD_REQUEST),
            ({"Content-Length": str(16 * 1024 + 1)}, HTTPStatus.BAD_REQUEST),
        ]:
            assert request("/api/memory/action", method="POST", payload=payload, role="user", headers=headers)[0][1] == status
        assert act.call_count == 1


def test_memory_action_errors_hide_foreign_existence_and_storage_details():
    from memory.repository import InvalidTransition, MemoryNotFound, ScopeDenied, VersionConflict
    from web.memory_management import MemoryStorageUnavailable
    with patch("web.memory_management.apply_memory_action") as act:
        for error, status in [
            (MemoryNotFound("foreign-memory"), HTTPStatus.NOT_FOUND),
            (ScopeDenied("foreign-memory and owner"), HTTPStatus.NOT_FOUND),
            (VersionConflict("foreign-version"), HTTPStatus.CONFLICT),
            (InvalidTransition("internal status"), HTTPStatus.CONFLICT),
            (MemoryStorageUnavailable("private-dsn-password"), HTTPStatus.SERVICE_UNAVAILABLE),
            (RuntimeError("private-dsn-password"), HTTPStatus.SERVICE_UNAVAILABLE),
        ]:
            act.side_effect = error
            response = request("/api/memory/action", method="POST", payload={}, role="user")[0]
            assert response[1] == status
            assert "foreign" not in response[0]["error"]
            assert "private-dsn-password" not in response[0]["error"]


def test_deeply_nested_request_is_a_safe_client_validation_error():
    handler = object.__new__(RequestHandler)
    body = ("[" * 2500 + "0" + "]" * 2500).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    with pytest.raises(ValueError, match="nesting is too deep"):
        handler._read_json_body()


def test_request_depth_limit_preserves_brackets_and_escaped_quotes_inside_text():
    handler = object.__new__(RequestHandler)
    payload = {"text": '[{\\"' * 300}
    body = json.dumps(payload).encode()
    handler.headers = {"Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    assert handler._read_json_body() == payload


def test_model_failure_events_are_sanitized_without_mutating_private_trace():
    from web.error_messages import public_worker_event
    event = {"type": "event", "data": {"event": "model.call.failed", "payload": {
        "error_message": "private-bearer-key", "route_attempts": [{"error": "private-bearer-key", "profile": "relay"}],
    }}}
    public = public_worker_event(event)
    assert "private-bearer-key" not in json.dumps(public)
    assert event["data"]["payload"]["error_message"] == "private-bearer-key"
    ordinary = {"type": "event", "data": {"event": "tool.call.completed", "payload": {"output_preview": "ok"}}}
    assert public_worker_event(ordinary) is ordinary
