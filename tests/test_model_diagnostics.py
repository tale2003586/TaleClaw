import asyncio
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import shutil
import ssl
import subprocess
import threading
import time

import httpx
import pytest

from web import model_diagnostics as diagnostics
from web.runtime_settings import RuntimeSettingsStore, SettingsConflictError, SettingsValidationError


def profile(name="primary", key="test-private-key"):
    return {"name": name, "provider": "openai", "api_key": key,
            "base_url": "https://provider.example/v1", "model": "manual-model"}


def draft(store):
    public = store.get_public()
    return {key: copy.deepcopy(public[key]) for key in ("revision", "default_profile", "profiles", "routes")}


@pytest.fixture
def store(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ={})
    store.update({"revision": 0, "default_profile": "primary", "profiles": [profile()], "routes": {}})
    return store


def test_preview_resolves_aliases_and_profile_fallbacks_without_clients_network_or_writes(store, monkeypatch):
    monkeypatch.setattr("models.model_pool.ModelPool.client_for_profile", lambda *args: pytest.fail("No client expected"))
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: pytest.fail("No DNS expected"))
    payload = draft(store)
    payload["profiles"][0]["fallbacks"] = ["backup"]
    payload["profiles"].append(profile("backup", "backup-private-key"))
    payload["routes"] = {"default": ["backup"], "summary": ["primary"]}
    original = store.path.read_bytes()
    preview = store.preview(payload)
    assert store.path.read_bytes() == original
    assert preview["revision"] == 1
    routes = {route["purpose"]: route for route in preview["routes"]}
    assert routes["reflection"]["source"] == "alias"
    assert routes["reflection"]["source_purpose"] == "summary"
    assert routes["reflection"]["profiles"] == [
        {"name": "primary", "provider": "openai", "model": "manual-model", "source": "route"},
        {"name": "backup", "provider": "openai", "model": "manual-model", "source": "profile_fallback", "fallback_of": "primary"},
    ]
    assert routes["coding"]["source"] == "default"
    assert "private-key" not in json.dumps(preview)
    assert payload["profiles"][1]["api_key"] == "backup-private-key"


def test_preview_initial_deployment_does_not_even_create_storage_directory(tmp_path):
    store = RuntimeSettingsStore(tmp_path / "absent", environ={})
    preview = store.preview({"revision": 0, "default_profile": "primary", "profiles": [profile()], "routes": {}})
    assert preview["configured"] is True
    assert not store.path.parent.exists()


@pytest.mark.parametrize("action", ["preview", "prepare_probe"])
def test_revision_checked_before_draft_validation_and_network(store, action, monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: pytest.fail("No DNS expected"))
    with pytest.raises(SettingsConflictError):
        getattr(store, action)({"revision": 0, **({"profile": {}} if action == "prepare_probe" else {"profiles": []})})


def test_probe_pins_ip_preserves_host_and_sni_and_uses_unsaved_draft(store, monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    submitted = draft(store)["profiles"][0]
    submitted["model"] = "not-yet-saved"
    original = store.path.read_bytes()
    calls = []

    def handler(request):
        calls.append(request)
        assert request.method == "GET"
        assert str(request.url) == "https://127.0.0.1/v1/models"
        assert request.headers["host"] == "provider.example"
        assert request.headers["authorization"] == "Bearer test-private-key"
        assert request.headers["accept-encoding"] == "identity"
        assert request.extensions["sni_hostname"] == "provider.example"
        return httpx.Response(200, json={"data": [{"id": "discovered-model"}, {"id": "discovered-model"}]})

    result = diagnostics.probe_models(store, {"revision": 1, "profile": submitted}, transport=httpx.MockTransport(handler))
    assert result["status"] == "ok"
    assert result["models"] == ["discovered-model"]
    assert result["elapsed_ms"] >= 0
    assert store.path.read_bytes() == original
    assert "test-private-key" not in json.dumps(result)
    assert len(calls) == 1


@pytest.mark.parametrize("code,status", [(401, "authentication_failed"), (403, "authentication_failed"),
                                         (404, "unsupported"), (405, "unsupported"), (501, "unsupported"),
                                         (302, "unavailable"), (429, "unavailable"), (500, "unavailable")])
def test_failure_responses_do_not_expose_provider_text_or_follow_redirects(store, monkeypatch, code, status):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(code, text="test-private-key raw upstream error", headers={"location": "https://evil.example"})

    result = diagnostics.probe_models(store, {"revision": 1, "profile": draft(store)["profiles"][0]}, transport=httpx.MockTransport(handler))
    assert result["status"] == status
    assert result["models"] == []
    assert len(requests) == 1
    assert "private-key" not in json.dumps(result)
    assert "upstream error" not in json.dumps(result)


def test_model_identifiers_bounded_and_secret_echo_filtered(store, monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    response = {"data": [{"id": "test-private-key"}, {"id": "x" * 201}, {"id": "bad\nname"}] + [{"id": f"model-{n}"} for n in range(205)]}
    result = diagnostics.probe_models(store, {"revision": 1, "profile": draft(store)["profiles"][0]},
                                      transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response)))
    assert result["status"] == "ok"
    assert len(result["models"]) == 200
    assert result["models"][0] == "model-0"
    assert result["truncated"] is True


def test_slow_body_uses_total_deadline(store, monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    monkeypatch.setattr(diagnostics, "PROBE_TIMEOUT_SECONDS", 0.05)

    class Drip(httpx.AsyncByteStream):
        async def __aiter__(self):
            while True:
                await asyncio.sleep(0.01)
                yield b" "

    started = time.monotonic()
    result = diagnostics.probe_models(store, {"revision": 1, "profile": draft(store)["profiles"][0]},
                                      transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Drip())))
    assert result["status"] == "unavailable"
    assert time.monotonic() - started < 0.5


def test_dns_deadline_does_not_wait_for_resolver_thread(monkeypatch):
    finished = threading.Event()

    def stuck(*args, **kwargs):
        finished.wait(1)
        return []

    monkeypatch.setattr(diagnostics.socket, "getaddrinfo", stuck)
    try:
        with pytest.raises(TimeoutError):
            diagnostics._resolve_target("provider.example", 443, time.monotonic() + 0.03)
    finally:
        finished.set()


def test_probe_rejects_credential_target_changes_before_resolving(store, monkeypatch):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: pytest.fail("Credential validation must precede DNS"))
    submitted = draft(store)["profiles"][0]
    submitted["base_url"] = "https://other.example/v1"
    with pytest.raises(SettingsValidationError, match="API Key"):
        diagnostics.probe_models(store, {"revision": 1, "profile": submitted})


@pytest.mark.parametrize("body,headers", [(b"x" * (diagnostics.MAX_RESPONSE_BYTES + 1), {}),
                                         (b"{}", {"content-encoding": "gzip"}),
                                         (b"not json", {}), (b"[]", {})])
def test_incompatible_or_oversized_response_is_fixed_failure(store, monkeypatch, body, headers):
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    result = diagnostics.probe_models(store, {"revision": 1, "profile": draft(store)["profiles"][0]},
                                      transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=httpx.ByteStream(body), headers=headers)))
    assert result["status"] == "invalid_response"
    assert result["models"] == []


def test_real_loopback_tls_uses_original_hostname_for_sni_and_certificate_validation(tmp_path, monkeypatch):
    """A certificate with no IP SAN proves the pinned connection keeps DNS TLS identity."""
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL is needed to generate the local test certificate")
    certificate, private_key = tmp_path / "relay.crt", tmp_path / "relay.key"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(private_key), "-out", str(certificate), "-subj", "/CN=relay.test",
                    "-addext", "subjectAltName=DNS:relay.test"], check=True, capture_output=True)
    captured = {}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            captured.update(path=self.path, host=self.headers["Host"], key=self.headers["Authorization"])
            body = b'{"data":[{"id":"local-model"}]}'
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, private_key)
    tls.set_servername_callback(lambda socket, name, context: captured.update(sni=name))
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    trusted = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(diagnostics, "_resolve_target", lambda *args: "127.0.0.1")
    submitted = profile(key="fake-loopback-key")
    submitted["base_url"] = f"https://relay.test:{server.server_port}/v1"
    try:
        result = diagnostics.probe_models(RuntimeSettingsStore(tmp_path / "settings", environ={}),
                                          {"revision": 0, "profile": submitted},
                                          transport=httpx.AsyncHTTPTransport(verify=trusted))
        assert result["status"] == "ok"
        assert result["models"] == ["local-model"]
        assert captured == {"sni": "relay.test", "host": f"relay.test:{server.server_port}",
                            "path": "/v1/models", "key": "Bearer fake-loopback-key"}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=1)
