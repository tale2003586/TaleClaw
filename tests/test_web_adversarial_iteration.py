"""Independent red-team regressions for web workbench iteration.

No real providers or credentials are used.  These tests exercise boundaries
using a temporary settings store and deliberately synthetic credentials.
"""

from __future__ import annotations

import copy
import asyncio
import io
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from http import HTTPStatus
from unittest.mock import Mock, patch

import pytest
import httpx

from models.model_pool import build_model_pool_from_env
from web.runtime_settings import (
    RuntimeSettingsStore,
    SettingsConflictError,
    SettingsValidationError,
)


FAKE_ORIGINAL_KEY = "FAKE-ADVERSARIAL-ORIGINAL-KEY"
FAKE_REPLACEMENT_KEY = "FAKE-ADVERSARIAL-REPLACEMENT-KEY"


def isolated_store(tmp_path):
    return RuntimeSettingsStore(tmp_path, environ={
        "LLM_PROVIDER": "relay",
        "LLM_PROVIDERS_JSON": json.dumps({"relay": {
            "provider": "openai", "api_key": FAKE_ORIGINAL_KEY,
            "base_url": "https://original.example/v1", "model": "original-model",
        }}),
    })


def current_draft(store):
    public = store.get_public()
    return {key: copy.deepcopy(public[key]) for key in (
        "revision", "profiles", "routes", "default_profile",
    )}


@pytest.mark.parametrize("identity_change", [
    {"provider": "another_vendor"},
    {"base_url": "https://unrelated.example/v1"},
    {"base_url": "http://original.example/v1"},
    {"base_url": "https://original.example/private/v1"},
    {"base_url": "https://original.example:444/v1"},
])
def test_changed_target_cannot_reuse_hidden_saved_key(tmp_path, identity_change):
    store = isolated_store(tmp_path)
    draft = current_draft(store)
    draft["profiles"][0].update(identity_change)
    with pytest.raises(SettingsValidationError):
        store.update(draft)
    assert not store.path.exists()
    assert store.get_public()["revision"] == 0


def test_rejected_key_migration_does_not_destroy_saved_configuration(tmp_path):
    store = isolated_store(tmp_path)
    store.update(current_draft(store))
    before = store.path.read_bytes()
    draft = current_draft(store)
    draft["profiles"][0]["base_url"] = "https://unrelated.example/v1"
    draft["profiles"][0]["api_key"] = "   "
    with pytest.raises(SettingsValidationError):
        store.update(draft)
    assert store.path.read_bytes() == before
    profile = build_model_pool_from_env(store.effective_environ()).profile_named("relay")
    assert profile.api_key == FAKE_ORIGINAL_KEY
    assert profile.base_url == "https://original.example/v1"


def test_explicit_replacement_key_allows_target_migration_without_secret_echo(tmp_path):
    store = isolated_store(tmp_path)
    draft = current_draft(store)
    draft["profiles"][0].update(
        base_url="https://unrelated.example/v1", api_key=FAKE_REPLACEMENT_KEY,
    )
    public = store.update(draft)
    profile = build_model_pool_from_env(store.effective_environ()).profile_named("relay")
    assert profile.api_key == FAKE_REPLACEMENT_KEY
    assert profile.base_url == "https://unrelated.example/v1"
    encoded = json.dumps(public)
    assert FAKE_ORIGINAL_KEY not in encoded
    assert FAKE_REPLACEMENT_KEY not in encoded
    assert public["profiles"][0]["has_api_key"] is True


def test_new_name_cannot_acquire_existing_provider_environment_key(tmp_path):
    store = isolated_store(tmp_path)
    draft = current_draft(store)
    draft["profiles"][0]["name"] = "other_profile"
    draft["default_profile"] = "other_profile"
    draft["routes"] = {"default": ["other_profile"]}
    with pytest.raises(SettingsValidationError):
        store.update(draft)
    assert not store.path.exists()


def test_stale_draft_cannot_rotate_current_secret_or_replace_target(tmp_path):
    store = isolated_store(tmp_path)
    stale = current_draft(store)
    current = copy.deepcopy(stale)
    current["profiles"][0]["api_key"] = FAKE_REPLACEMENT_KEY
    store.update(current)
    before = store.path.read_bytes()
    stale["profiles"][0].update(base_url="https://unrelated.example/v1", api_key="FAKE-STALE")
    with pytest.raises(SettingsConflictError):
        store.update(stale)
    assert store.path.read_bytes() == before


def web_request(path, *, payload=None, role="user", headers=None, authorized=True):
    from web.auth_store import AuthenticatedUser
    from web.server import RequestHandler

    handler = object.__new__(RequestHandler)
    handler.path = path
    body = json.dumps(payload).encode() if payload is not None else b""
    handler.headers = {
        "Content-Type": "application/json", "Content-Length": str(len(body)),
        "Host": "localhost:8000", **(headers or {}),
    }
    handler.rfile = io.BytesIO(body)
    handler.auth_user = AuthenticatedUser(user_id="alice", role=role)
    handler._authorize = Mock(return_value=authorized)
    handler.agent_service = Mock()
    responses = []
    handler._send_json = lambda value, status=HTTPStatus.OK, headers=None: responses.append(
        (value, status, headers or {}),
    )
    handler.do_POST()
    handler.agent_service.ensure_started.assert_not_called()
    return responses


@pytest.mark.parametrize("operation", ["probe", "preview"])
def test_user_cannot_probe_or_preview_shared_secret_configuration(operation):
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        response = web_request(f"/api/settings/models/{operation}", payload={})
    assert response[0][1] == HTTPStatus.FORBIDDEN
    store.assert_not_called()


@pytest.mark.parametrize("operation", ["probe", "preview"])
@pytest.mark.parametrize("headers", [
    {"Origin": "https://unrelated.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Content-Type": "text/plain"},
])
def test_cross_origin_diagnostics_never_reach_settings_or_network(operation, headers):
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        response = web_request(f"/api/settings/models/{operation}",
                               payload={}, role="admin", headers=headers)
    assert response[0][1] in {HTTPStatus.FORBIDDEN, HTTPStatus.UNSUPPORTED_MEDIA_TYPE}
    store.assert_not_called()


@pytest.mark.parametrize("headers", [
    {"Origin": "https://unrelated.example"},
    {"Origin": "null"},
    {"Sec-Fetch-Site": "cross-site"},
    {"Content-Type": "text/plain"},
])
def test_cross_origin_memory_mutations_stop_before_scope_or_storage_resolution(headers):
    with patch("web.memory_view.load_memory_session") as load_session:
        response = web_request("/api/memory/action", payload={
            "memory_id": "FAKE-MEMORY", "action": "revoke", "expected_version": 1,
        }, headers=headers)
    assert response[0][1] in {HTTPStatus.FORBIDDEN, HTTPStatus.UNSUPPORTED_MEDIA_TYPE}
    load_session.assert_not_called()


@pytest.mark.parametrize("path", [
    "/api/settings/models/probe", "/api/settings/models/preview", "/api/memory/action",
])
def test_unauthenticated_iteration_endpoints_stop_before_runtime_or_storage(path):
    with patch("web.runtime_settings.RuntimeSettingsStore") as store:
        assert web_request(path, payload={}, authorized=False) == []
    store.assert_not_called()


class ProbeBytes(httpx.AsyncByteStream):
    def __init__(self, content):
        self.content = content

    async def __aiter__(self):
        yield self.content


def probe_payload(store):
    draft = current_draft(store)
    return {"revision": draft["revision"], "profile": draft["profiles"][0]}


def run_fake_probe(monkeypatch, store, handler, payload=None):
    from web import model_diagnostics

    monkeypatch.setattr(model_diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    return model_diagnostics.probe_models(
        store, payload or probe_payload(store), transport=httpx.MockTransport(handler),
    )


def test_probe_pins_dns_result_and_preserves_original_host_and_tls_name(monkeypatch, tmp_path):
    store = isolated_store(tmp_path)
    captured = []

    def handler(request):
        captured.append(request)
        return httpx.Response(200, stream=ProbeBytes(b'{"data":[{"id":"discovered-model"}]}'))

    result = run_fake_probe(monkeypatch, store, handler)
    assert result["status"] == "ok" and result["models"] == ["discovered-model"]
    request = captured[0]
    assert request.url.host == "127.0.0.1"
    assert request.headers["host"] == "original.example"
    assert request.extensions["sni_hostname"] == "original.example"
    assert request.headers["authorization"] == f"Bearer {FAKE_ORIGINAL_KEY}"
    assert request.headers["accept-encoding"] == "identity"
    assert not store.path.exists()


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_probe_never_follows_redirect_to_a_credential_collection_endpoint(monkeypatch, tmp_path, status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={"Location": "https://unrelated.example/capture"},
                              stream=ProbeBytes(FAKE_ORIGINAL_KEY.encode()))

    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), handler)
    assert len(requests) == 1
    assert result["status"] == "unavailable"
    assert FAKE_ORIGINAL_KEY not in json.dumps(result)


@pytest.mark.parametrize("status", [401, 403, 500])
def test_probe_hides_provider_error_bodies_and_secret_values(monkeypatch, tmp_path, status):
    def handler(request):
        return httpx.Response(status, stream=ProbeBytes(json.dumps({
            "error": FAKE_ORIGINAL_KEY, "request_headers": dict(request.headers),
        }).encode()))

    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), handler)
    assert result["status"] in {"authentication_failed", "unavailable"}
    assert FAKE_ORIGINAL_KEY not in json.dumps(result)
    assert "original.example" not in json.dumps(result)


def test_probe_hides_timeout_exception_text(monkeypatch, tmp_path):
    def handler(request):
        raise httpx.ReadTimeout(f"https://private.example/{FAKE_ORIGINAL_KEY}", request=request)

    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), handler)
    assert result["status"] == "unavailable"
    assert FAKE_ORIGINAL_KEY not in json.dumps(result)
    assert "private.example" not in json.dumps(result)


def test_stale_probe_is_rejected_before_dns_or_transport(monkeypatch, tmp_path):
    from web import model_diagnostics

    store = isolated_store(tmp_path)
    stale = probe_payload(store)
    store.update(current_draft(store))
    resolver = Mock(side_effect=AssertionError("Stale probe reached DNS"))
    monkeypatch.setattr(model_diagnostics, "_resolve_target", resolver)
    transport = httpx.MockTransport(lambda request: pytest.fail("Stale probe reached transport"))
    with pytest.raises(SettingsConflictError):
        model_diagnostics.probe_models(store, stale, transport=transport)
    resolver.assert_not_called()


@pytest.mark.parametrize("address", [
    "169.254.169.254", "100.100.100.200", "fd00:ec2::254", "0.0.0.0", "::",
    "224.0.0.1", "::ffff:169.254.169.254", "::ffff:100.100.100.200",
])
def test_metadata_and_special_address_variants_cannot_be_probed(address):
    from web.model_diagnostics import _safe_ip

    with pytest.raises(SettingsValidationError):
        _safe_ip(address)


def test_mixed_dns_answers_are_rejected_before_any_credential_request(monkeypatch):
    from web import model_diagnostics
    import socket

    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 443)),
    ])
    with pytest.raises(SettingsValidationError):
        model_diagnostics._resolve_target("example.invalid", 443, time.monotonic() + 1)


def test_probe_uses_total_deadline_against_slow_chunked_response(monkeypatch, tmp_path):
    from web import model_diagnostics

    class SlowBytes(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

    monkeypatch.setattr(model_diagnostics, "PROBE_TIMEOUT_SECONDS", 0.04)
    started = time.monotonic()
    result = run_fake_probe(monkeypatch, isolated_store(tmp_path),
                            lambda request: httpx.Response(200, stream=SlowBytes()))
    elapsed = time.monotonic() - started
    assert result["status"] == "unavailable"
    assert elapsed < 0.5


def test_compressed_response_is_rejected_without_reading_or_decompressing(monkeypatch, tmp_path):
    class NeverRead(httpx.AsyncByteStream):
        async def __aiter__(self):
            pytest.fail("Compressed response was read before being rejected")
            yield b""

    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), lambda request: httpx.Response(
        200, headers={"Content-Encoding": "gzip"}, stream=NeverRead(),
    ))
    assert result["status"] == "invalid_response"


def test_deeply_nested_provider_json_is_a_safe_invalid_response(monkeypatch, tmp_path):
    nested = b'{"data":' + b"[" * 20000 + b"0" + b"]" * 20000 + b"}"
    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), lambda request: httpx.Response(
        200, stream=ProbeBytes(nested),
    ))
    assert result["status"] == "invalid_response"


def test_discovered_model_allowlist_drops_secret_control_character_and_oversized_ids(monkeypatch, tmp_path):
    data = {"data": [
        {"id": FAKE_ORIGINAL_KEY}, {"id": "bad\nmodel"}, {"id": "x" * 201},
        {"id": "safe-model", "api_key": FAKE_ORIGINAL_KEY}, {"id": "safe-model"}, {},
    ]}
    result = run_fake_probe(monkeypatch, isolated_store(tmp_path), lambda request: httpx.Response(
        200, stream=ProbeBytes(json.dumps(data).encode()),
    ))
    assert result["models"] == ["safe-model"]
    assert FAKE_ORIGINAL_KEY not in json.dumps(result)


def memory_row(**changes):
    from memory.domain import MemoryItem

    now = datetime.now(timezone.utc)
    return asdict(MemoryItem(**{
        "id": "FAKE-MEMORY", "owner_scope": "user", "owner_id": "alice",
        "kind": "fact", "content": "FAKE reviewed memory", "normalized_content": "fake reviewed memory",
        "status": "active", "confidence": .8, "salience": .8,
        "valid_from": now - timedelta(days=1), **changes,
    }))


class LockedMemoryConnection:
    def __init__(self, row, *, fail_outbox=False):
        self.row = row
        self.fail_outbox = fail_outbox
        self.calls = []
        self.committed = self.rolled_back = False

    def execute(self, query, params=()):
        self.calls.append((query, params))
        if "FROM memory_items" in query:
            return SimpleNamespace(fetchone=lambda: self.row)
        if "INSERT INTO memory_index_outbox" in query and self.fail_outbox:
            raise RuntimeError("FAKE-OUTBOX-FAILURE")
        return SimpleNamespace(rowcount=1, fetchall=lambda: [])

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def scoped_memory_transition(connection, *, expected_version=1):
    from memory.commands import MemoryTransition
    from memory.domain import MemoryOwnerScope, MemoryStatus, OwnerKey
    from memory.postgres_repository import PostgresMemoryRepository

    repository = object.__new__(PostgresMemoryRepository)
    repository._connect = lambda: connection
    return repository.transition_scoped(
        MemoryTransition("FAKE-MEMORY", MemoryStatus.REVOKED, expected_version),
        (OwnerKey(MemoryOwnerScope.USER, "alice"),), expected_status=MemoryStatus.ACTIVE,
    )


@pytest.mark.parametrize("owner", [
    {"owner_scope": "user", "owner_id": "bob"},
    {"owner_scope": "project", "owner_id": "shared-repo"},
    {"owner_scope": "task", "owner_id": "unverified-task"},
])
def test_foreign_scope_is_denied_inside_row_lock_before_version_or_evidence(owner):
    from memory.repository import ScopeDenied

    connection = LockedMemoryConnection(memory_row(version=99, **owner))
    with pytest.raises(ScopeDenied):
        scoped_memory_transition(connection, expected_version=1)
    assert connection.rolled_back and not connection.committed
    assert len(connection.calls) == 1
    assert "FOR UPDATE" in connection.calls[0][0]


def test_memory_browser_version_cannot_be_replaced_by_current_database_version():
    from memory.repository import VersionConflict

    connection = LockedMemoryConnection(memory_row(version=99))
    with pytest.raises(VersionConflict):
        scoped_memory_transition(connection, expected_version=1)
    assert connection.rolled_back and not connection.committed
    assert len(connection.calls) == 1


def test_memory_outbox_failure_rolls_back_truth_state_and_does_not_report_success():
    connection = LockedMemoryConnection(memory_row(), fail_outbox=True)
    with pytest.raises(RuntimeError, match="FAKE-OUTBOX-FAILURE"):
        scoped_memory_transition(connection)
    assert connection.rolled_back and not connection.committed
    assert not any("memory_evidence" in query for query, _ in connection.calls)


def test_memory_transition_commits_after_state_outbox_and_evidence_are_consistent():
    connection = LockedMemoryConnection(memory_row())
    updated, evidence = scoped_memory_transition(connection)
    assert updated.status.value == "revoked" and updated.version == 2 and evidence == []
    assert connection.committed and not connection.rolled_back
    operations = [query for query, _ in connection.calls]
    assert "FOR UPDATE" in operations[0]
    state_index = next(i for i, query in enumerate(operations) if "UPDATE memory_items" in query)
    outbox_index = next(i for i, query in enumerate(operations) if "INSERT INTO memory_index_outbox" in query)
    evidence_index = next(i for i, query in enumerate(operations) if "FROM memory_evidence" in query)
    assert state_index < outbox_index < evidence_index
    assert connection.calls[outbox_index][1][1:4] == ("FAKE-MEMORY", 2, "delete")


def test_missing_and_forbidden_memory_actions_have_identical_public_responses():
    from memory.repository import MemoryNotFound, ScopeDenied

    payload = {"id": "FAKE-MEMORY", "action": "revoke", "expected_version": 1}
    responses = []
    for error in (MemoryNotFound(FAKE_ORIGINAL_KEY), ScopeDenied(FAKE_ORIGINAL_KEY)):
        with patch("web.memory_management.apply_memory_action", side_effect=error):
            responses.append(web_request("/api/memory/action", payload=payload)[0])
    assert responses[0] == responses[1]
    assert responses[0][1] == HTTPStatus.NOT_FOUND
    assert FAKE_ORIGINAL_KEY not in json.dumps(responses)


def fake_provider_error(kind):
    if kind == "httpx":
        return httpx.ReadTimeout(f"FAKE private provider failure: {FAKE_ORIGINAL_KEY}")
    if kind == "route":
        from models.model_pool import ModelRouteError
        return ModelRouteError(
            purpose="chat", attempts=[{"error": FAKE_ORIGINAL_KEY}],
            message=f"All providers failed: {FAKE_ORIGINAL_KEY}",
        )
    from openai import AuthenticationError
    response = httpx.Response(401, request=httpx.Request("POST", "https://relay.invalid/v1/chat/completions"))
    return AuthenticationError(f"Incorrect API key: {FAKE_ORIGINAL_KEY}", response=response,
                               body={"error": {"message": FAKE_ORIGINAL_KEY}})


@pytest.mark.parametrize("kind", ["openai", "httpx", "route"])
def test_public_runtime_error_does_not_echo_upstream_credentials(kind):
    from web.server import _friendly_runtime_error

    public = _friendly_runtime_error(fake_provider_error(kind))
    assert public
    assert FAKE_ORIGINAL_KEY not in public


@pytest.mark.parametrize("kind", ["openai", "httpx", "route"])
def test_worker_result_boundary_does_not_echo_upstream_credentials(monkeypatch, kind):
    from web import run_worker

    error = fake_provider_error(kind)
    def fail(request, emit, cancelled):
        raise error

    monkeypatch.setattr(run_worker.os, "setsid", lambda: None)
    monkeypatch.setattr(run_worker.ctypes, "CDLL", lambda *args, **kwargs: SimpleNamespace(prctl=lambda *args: 0))
    monkeypatch.setattr(run_worker.importlib, "import_module", lambda *args: SimpleNamespace(fail=fail))
    monkeypatch.setattr(run_worker, "_cleanup_descendants", lambda: True)
    monkeypatch.setenv("TALECLAW_RUN_ID", "FAKE-PREVIOUS-RUN")
    results = []
    sender = SimpleNamespace(send=lambda value: results.append(value), close=lambda: None)
    run_worker.worker_entry("FAKE_PROVIDER_MODULE:fail", {"run_id": "FAKE-RUN", "attempt": 1}, sender,
                            SimpleNamespace(is_set=lambda: False))
    assert results[0]["result"]["status"] == "failed"
    assert FAKE_ORIGINAL_KEY not in json.dumps(results)
