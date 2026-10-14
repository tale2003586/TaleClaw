import copy
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
import json
import multiprocessing
import os
import stat

import pytest

from models.model_pool import build_model_pool_from_env
from web.runtime_settings import (
    RuntimeSettingsStore, SettingsConflictError, SettingsStorageError,
    SettingsValidationError, effective_environ,
)


def environment():
    return {
        "LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "old-private-key",
        "MIMO_API_KEY": "other-private-key", "LLM_ROUTE_CHAT": "mimo,deepseek",
        "LLM_ROUTE_FALLBACK": "deepseek", "DEEPSEEK_MAX_INPUT_TOKENS": "12000",
        "DEEPSEEK_USER_AGENT": "legacy-agent", "UNRELATED_SETTING": "unchanged",
    }


def update_payload(public):
    return {key: copy.deepcopy(public[key]) for key in (
        "revision", "default_profile", "profiles", "routes",
    )}


def relay(name="relay", key="new-private-key"):
    return {"name": name, "provider": "openai", "api_key": key,
            "base_url": "https://relay.example/v1", "model": "relay-model",
            "wire_api": "responses"}


def save_in_another_process(arguments):
    root, env, payload = arguments
    try:
        return RuntimeSettingsStore(root, environ=env).update(payload)["revision"]
    except SettingsConflictError:
        return "conflict"


def test_environment_profiles_and_routes_are_public_without_keys(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    public = store.get_public()
    assert public["configured"] is True
    assert public["revision"] == 0
    assert public["default_profile"] == "deepseek"
    assert public["routes"]["chat"] == ["mimo", "deepseek"]
    assert public["profiles"][0]["has_api_key"] is True
    encoded = json.dumps(public)
    assert "old-private-key" not in encoded
    assert "other-private-key" not in encoded
    assert not store.path.exists()
    assert store.effective_environ() == environment()


def test_save_preserves_blank_keys_and_survives_new_store(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    payload = update_payload(store.get_public())
    payload["profiles"][0]["api_key"] = ""
    payload["profiles"][0]["model"] = "new-model"
    saved = store.update(payload)
    assert saved["revision"] == 1
    assert saved["configured"] is True
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.with_name("settings.json.lock").stat().st_mode) == 0o600
    restarted = RuntimeSettingsStore(tmp_path, environ={"DEEPSEEK_API_KEY": "changed-deployment-key"})
    pool = build_model_pool_from_env(restarted.effective_environ())
    assert pool.profile_named("deepseek").api_key == "old-private-key"
    assert pool.profile_named("deepseek").model == "new-model"
    assert restarted.get_public() == saved


def test_new_profile_rotation_removal_and_route_override_take_effect(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    before = store.effective_environ()
    old_pool = build_model_pool_from_env(before)
    payload = {"revision": 0, "default_profile": "relay", "profiles": [relay()],
               "routes": {"chat": ["relay"], "custom_task": ["relay"]}}
    store.update(payload)
    after = store.effective_environ()
    pool = build_model_pool_from_env(after)
    assert pool.default_profile == "relay"
    assert list(pool.profiles) == ["relay"]
    assert pool.route_profile_names("chat") == ["relay"]
    assert pool.route_profile_names("custom_task") == ["relay"]
    assert pool.route_profile_names("summary") == ["relay"]
    assert pool.profile_for().wire_api == "responses"
    assert after["UNRELATED_SETTING"] == "unchanged"
    assert after["DEEPSEEK_API_KEY"] == "old-private-key"
    # Existing runs and previously captured snapshots remain independent.
    assert before == environment()
    assert old_pool.route_profile_names("chat") == ["mimo", "deepseek"]
    payload = update_payload(store.get_public())
    payload["profiles"][0]["api_key"] = "rotated-private-key"
    store.update(payload)
    assert build_model_pool_from_env(store.effective_environ()).profile_for().api_key == "rotated-private-key"
    assert pool.profile_for().api_key == "new-private-key"


def test_cleared_advanced_settings_do_not_reappear_from_environment(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    payload = update_payload(store.get_public())
    payload["profiles"][0]["max_input_tokens"] = None
    payload["profiles"][0]["user_agent"] = ""
    store.update(payload)
    profile = build_model_pool_from_env(store.effective_environ()).profile_named("deepseek")
    assert profile.max_input_tokens is None
    assert profile.user_agent == ""


def test_provider_json_preserves_env_key_resolution_and_advanced_fields(tmp_path):
    env = {
        "LLM_PROVIDER": "relay", "PRIVATE_RELAY_KEY": "env-private-key",
        "LLM_PROVIDERS_JSON": json.dumps({"relay": {
            "provider": "openai", "api_key_env": "PRIVATE_RELAY_KEY",
            "base_url": "https://relay.example/v1", "model": "json-model",
            "wire_api": "responses", "context_window_tokens": 200000,
            "supports_thinking": True, "thinking_param": "reasoning",
            "thinking_value": {"effort": "high"},
        }}),
        "LLM_ROUTES_JSON": json.dumps({"coding": ["relay"]}),
    }
    store = RuntimeSettingsStore(tmp_path, environ=env)
    public = store.get_public()
    assert public["profiles"][0]["thinking_value"] == {"effort": "high"}
    store.update(update_payload(public))
    profile = build_model_pool_from_env(store.effective_environ()).profile_for()
    assert profile.api_key == "env-private-key"
    assert profile.context_window_tokens == 200000
    assert profile.supports_thinking is True


@pytest.mark.parametrize("env", [
    {}, {"LLM_PROVIDERS_JSON": "malformed-private-key"},
    {"LLM_PROVIDER": "deepseek", "DEEPSEEK_API_KEY": "private", "LLM_ROUTE_CHAT": "removed"},
])
def test_unconfigured_environment_can_be_repaired_without_secret_errors(tmp_path, env):
    store = RuntimeSettingsStore(tmp_path, environ=env)
    public = store.get_public()
    assert public["configured"] is False
    assert "private" not in public.get("error", "")
    saved = store.update({"revision": 0, "default_profile": "relay", "profiles": [relay()], "routes": {}})
    assert saved["configured"] is True
    assert saved["routes"]["default"] == ["relay"]


@pytest.mark.parametrize("change", [
    lambda value: value.update(profiles=[]),
    lambda value: value.update(default_profile="missing"),
    lambda value: value.update(routes={"chat": ["removed"]}),
    lambda value: value.update(routes={"chat": []}),
    lambda value: value["profiles"][0].update(api_key=""),
    lambda value: value["profiles"][0].update(wire_api="invalid"),
    lambda value: value["profiles"][0].update(wire_api=[]),
    lambda value: value["profiles"][0].update(max_input_tokens=-1),
    lambda value: value["profiles"][0].update(max_output_tokens="abc"),
    lambda value: value["profiles"][0].update(base_url="https://private-key@example.test/v1"),
    lambda value: value["profiles"][0].update(base_url="https://example.test/v1?key=private-key"),
    lambda value: value["profiles"][0].update(supports_thinking=True),
    lambda value: value["profiles"][0].update(fallbacks=["removed"]),
    lambda value: value["profiles"].append(relay("RE-LAY")),
])
def test_invalid_settings_do_not_write_or_expose_credentials(tmp_path, change):
    store = RuntimeSettingsStore(tmp_path, environ={})
    payload = {"revision": 0, "default_profile": "re_lay", "profiles": [relay("re_lay")], "routes": {}}
    change(payload)
    with pytest.raises(SettingsValidationError) as failure:
        store.update(payload)
    assert "private-key" not in str(failure.value)
    assert not store.path.exists()


def test_stale_revision_and_concurrent_writers_cannot_overwrite(tmp_path):
    stores = [RuntimeSettingsStore(tmp_path, environ=environment()) for _ in range(2)]
    payload = update_payload(stores[0].get_public())

    def save(store):
        try:
            return store.update(payload)["revision"]
        except SettingsConflictError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(save, stores))
    assert sorted(results, key=str) == [1, "conflict"]
    assert stores[0].get_public()["revision"] == 1
    with pytest.raises(SettingsConflictError):
        stores[0].update(payload)


def test_separate_server_processes_share_revision_lock(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    arguments = (str(tmp_path), environment(), update_payload(store.get_public()))
    with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as executor:
        results = list(executor.map(save_in_another_process, [arguments, arguments]))
    assert sorted(results, key=str) == [1, "conflict"]
    assert store.get_public()["revision"] == 1


def test_storage_corruption_fails_closed_without_raw_contents(tmp_path):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    store.path.parent.mkdir()
    store.path.write_text("{private-key", encoding="utf-8")
    for action in (store.get_public, store.effective_environ):
        with pytest.raises(SettingsStorageError) as failure:
            action()
        assert "private-key" not in str(failure.value)


@pytest.mark.parametrize("change", [
    lambda payload: payload.update(default_profile=[]),
    lambda payload: payload["profiles"][0].update(provider_api_key="unknown-private-key"),
    lambda payload: payload["profiles"][0].update(base_url="https://private-key@example.test"),
    lambda payload: payload.update(routes={"default": ["missing"]}),
])
def test_corrupt_stored_fields_do_not_reach_public_api(tmp_path, change):
    store = RuntimeSettingsStore(tmp_path, environ={})
    store.update({"revision": 0, "default_profile": "relay", "profiles": [relay()], "routes": {}})
    persisted = json.loads(store.path.read_text())
    change(persisted)
    store.path.write_text(json.dumps(persisted), encoding="utf-8")
    for action in (store.get_public, store.effective_environ):
        with pytest.raises(SettingsStorageError) as failure:
            action()
        assert "private-key" not in str(failure.value)


def test_module_effective_environment_uses_shared_path_without_mutation(tmp_path, monkeypatch):
    env = environment()
    env["TALECLAW_SETTINGS_PATH"] = str(tmp_path / "shared" / "config.json")
    store = RuntimeSettingsStore(environ=env)
    store.update({"revision": 0, "default_profile": "relay", "profiles": [relay()], "routes": {}})
    monkeypatch.setenv("TALECLAW_SETTINGS_PATH", env["TALECLAW_SETTINGS_PATH"])
    process_before = dict(os.environ)
    copied = dict(env)
    result = effective_environ(copied)
    assert copied == env
    assert dict(os.environ) == process_before
    assert build_model_pool_from_env(result).default_profile == "relay"


def test_validation_constructs_no_api_clients(tmp_path, monkeypatch):
    def fail_client(*args, **kwargs):
        raise AssertionError("Saving settings must never create clients or call providers")
    monkeypatch.setattr("models.model_pool.ModelPool.client_for_profile", fail_client)
    store = RuntimeSettingsStore(tmp_path, environ={})
    store.update({"revision": 0, "default_profile": "relay", "profiles": [relay()], "routes": {}})
    assert store.get_public()["configured"] is True


@pytest.mark.parametrize("frozen_snapshot", [False, True])
def test_worker_rebuilds_imported_pool_from_one_run_snapshot(tmp_path, monkeypatch, frozen_snapshot):
    import sys
    import threading
    from types import ModuleType
    from web import run_worker

    env = {"TALECLAW_SETTINGS_PATH": str(tmp_path / "settings.json")}
    store = RuntimeSettingsStore(environ=env)
    store.update({"revision": 0, "default_profile": "relay", "profiles": [relay()], "routes": {}})
    first = store.effective_environ()
    payload = update_payload(store.get_public())
    payload["profiles"][0]["model"] = "second-model"
    store.update(payload)

    bootstrap = ModuleType("applications.bootstrap")
    bootstrap._MODEL_POOL = object()  # A pre-imported pool must be replaced.
    bootstrap._MODEL_HEALTHCHECK_RESULTS = ["stale"]
    bootstrap.build_runtime = lambda: pytest.fail("A cancelled run should not build a runtime")
    monkeypatch.setitem(sys.modules, "applications.bootstrap", bootstrap)
    monkeypatch.setattr(os, "environ", dict(env))
    monkeypatch.setattr(run_worker, "_MODEL_ENVIRONMENT", first if frozen_snapshot else None)
    cancelled = threading.Event()
    cancelled.set()
    assert run_worker.run_agent({}, lambda event: None, cancelled) == {"status": "stopped", "reply": ""}
    assert bootstrap._MODEL_POOL.profile_for().model == ("relay-model" if frozen_snapshot else "second-model")
    assert bootstrap._MODEL_HEALTHCHECK_RESULTS is None


@pytest.mark.parametrize("name", ["_Relay", "-Relay", "Relay-1", "a" * 64])
def test_permitted_profile_names_round_trip(tmp_path, name):
    store = RuntimeSettingsStore(tmp_path, environ=environment())
    public = store.update({"revision": 0, "default_profile": name,
                           "profiles": [relay(name)], "routes": {"default": [name]}})
    normalized = name.lower().replace("-", "_")
    assert public["default_profile"] == normalized
    restored = RuntimeSettingsStore(tmp_path, environ=environment())
    assert restored.get_public()["profiles"][0]["name"] == normalized
    assert build_model_pool_from_env(restored.effective_environ()).profile_named(normalized).model == "relay-model"
