"""Private, versioned model settings shared by the web server and its workers.

The environment remains the initial source of truth until the first save.
Thereafter a complete model snapshot overrides it, without changing the web
server's process environment or retaining model clients between runs.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SETTINGS_PATH_ENV = "TALECLAW_SETTINGS_PATH"
_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}")
_TOKEN_FIELDS = (
    "context_window_tokens", "max_input_tokens", "max_output_tokens",
    "output_reserve_tokens",
)
_STRING_FIELDS = (
    "user_agent", "max_tokens_param", "tokenizer_model", "thinking_param",
)
_BOOLEAN_FIELDS = ("bpe_tokenizer_enabled", "supports_thinking")
_PROFILE_FIELDS = {
    "name", "provider", "api_key", "base_url", "model", "wire_api",
    "thinking_value", "fallbacks", *_STRING_FIELDS, *_TOKEN_FIELDS, *_BOOLEAN_FIELDS,
}
_PROVIDER_SUFFIXES = (
    "BASE_URL", "MODEL", "USER_AGENT", "MAX_TOKENS_PARAM", "WIRE_API",
    "CONTEXT_WINDOW_TOKENS", "CONTEXT_LIMIT", "MAX_INPUT_TOKENS",
    "MAX_OUTPUT_TOKENS", "OUTPUT_RESERVE_TOKENS", "TOKENIZER_MODEL",
    "BPE_TOKENIZER_ENABLED", "TOKENIZER_ENABLED", "SUPPORTS_THINKING",
    "THINKING_PARAM", "THINKING_VALUE",
)


class SettingsValidationError(ValueError):
    """Public validation text contains field names, never submitted values."""


class SettingsConflictError(SettingsValidationError):
    pass


class SettingsStorageError(RuntimeError):
    pass


def _name(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _NAME.fullmatch(value.strip()):
        raise SettingsValidationError(f"{field} must contain letters, numbers, underscores or hyphens.")
    return value.strip().lower().replace("-", "_")


def _chain(value: Any, field: str) -> list[str]:
    if not isinstance(value, list) or not value:
        raise SettingsValidationError(f"{field} must contain at least one profile.")
    return list(dict.fromkeys(_name(item, field) for item in value))


def _profile_snapshot(profile) -> dict[str, Any]:
    result = asdict(profile)
    result["fallbacks"] = list(result["fallbacks"])
    return result


def _target_identity(profile: Mapping[str, Any]) -> tuple[str, str, str, str, int, str]:
    """Keys are bound to a complete normalized target, including its API path."""
    url = urlsplit(profile["base_url"])
    host = (url.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
    return (profile["name"], profile["provider"], url.scheme.lower(), host,
            url.port if url.port is not None else (443 if url.scheme.lower() == "https" else 80), url.path.rstrip("/"))


def _valid_stored_snapshot(payload: Any) -> bool:
    """Check storage without importing runtime/config into the worker boundary."""
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1 or type(payload.get("revision")) is not int
            or payload["revision"] < 1 or not isinstance(payload.get("profiles"), list)
            or not 0 < len(payload["profiles"]) <= 100 or not isinstance(payload.get("routes"), dict)):
        return False
    names = set()
    for profile in payload["profiles"]:
        if not isinstance(profile, dict) or set(profile) != _PROFILE_FIELDS:
            return False
        for field in ("name", "provider", "api_key", "base_url", "model", "wire_api", *_STRING_FIELDS):
            if not isinstance(profile[field], str) or any(char in profile[field] for char in "\r\n\x00"):
                return False
        for field in ("name", "provider"):
            if not _NAME.fullmatch(profile[field]) or _name(profile[field], field) != profile[field]:
                return False
        if (profile["name"] in names or not profile["api_key"] or not profile["base_url"] or not profile["model"]
                or profile["wire_api"] not in {"chat_completions", "responses"}
                or any(type(profile[field]) is not bool for field in _BOOLEAN_FIELDS)
                or not isinstance(profile["fallbacks"], list)
                or (profile["supports_thinking"] and not profile["thinking_param"])):
            return False
        try:
            url = urlsplit(profile["base_url"])
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None or url.query or url.fragment):
                return False
            if url.port == 0:
                return False
        except ValueError:
            return False
        for field in _TOKEN_FIELDS:
            value = profile[field]
            if value is not None and (type(value) is not int or not 0 < value <= 100_000_000):
                return False
        names.add(profile["name"])
    if (not isinstance(payload.get("default_profile"), str)
            or payload["default_profile"] not in names or "default" not in payload["routes"]):
        return False
    for purpose, chain in payload["routes"].items():
        if not isinstance(purpose, str) or not _NAME.fullmatch(purpose) or not isinstance(chain, list) or not chain:
            return False
        if any(not isinstance(name, str) or name not in names for name in chain):
            return False
    for profile in payload["profiles"]:
        if any(not isinstance(name, str) or name not in names for name in profile["fallbacks"]):
            return False
    try:
        json.dumps(payload, allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return False
    return True


def _model_environment(base: Mapping[str, str], snapshot: dict) -> dict[str, str]:
    env = dict(base)
    # Per-purpose env variables otherwise take precedence over the JSON routes.
    for key in list(env):
        if key.startswith("LLM_ROUTE_"):
            env[key] = ""
    # Explicitly cleared advanced fields must not reappear from legacy env vars.
    # API-key env vars remain private and available to unrelated embedding clients.
    prefixes = {"LLM"}
    for profile in snapshot["profiles"]:
        for value in (profile["name"], profile["provider"]):
            prefixes.add(re.sub(r"[^A-Z0-9]+", "_", value.upper()).strip("_"))
    for prefix in prefixes:
        for suffix in _PROVIDER_SUFFIXES:
            env[f"{prefix}_{suffix}"] = ""
    env["LLM_PROVIDER"] = snapshot["default_profile"]
    env["LLM_DEFAULT_PROVIDER"] = snapshot["default_profile"]
    env["LLM_PROVIDERS_JSON"] = json.dumps({
        profile["name"]: {key: value for key, value in profile.items() if key != "name"}
        for profile in snapshot["profiles"]
    }, ensure_ascii=False, allow_nan=False)
    env["LLM_ROUTES_JSON"] = json.dumps(snapshot["routes"], ensure_ascii=False)
    return env


def _validated_pool(snapshot: dict, environment: Mapping[str, str] | None = None):
    """Resolve every runtime route without constructing any provider clients."""
    from models.model_pool import ROUTE_ENV_NAMES, build_model_pool_from_env
    env = _model_environment({}, snapshot) if environment is None else dict(environment)
    env.setdefault("LLM_PROVIDER", "deepseek")
    pool = build_model_pool_from_env(env)
    for purpose in set(ROUTE_ENV_NAMES) | set(pool.routes):
        if not pool.route_profile_names(purpose):
            raise RuntimeError("Empty model route")
    for profile in pool.profiles.values():
        for fallback in profile.fallbacks:
            pool.profile_named(fallback)
    return pool


class RuntimeSettingsStore:
    def __init__(self, root: str | Path = ROOT, *,
                 environ: Mapping[str, str] | None = None,
                 path: str | Path | None = None) -> None:
        self.environ = dict(os.environ if environ is None else environ)
        configured_path = path or self.environ.get(SETTINGS_PATH_ENV)
        self.path = Path(configured_path) if configured_path else Path(root) / ".runtime" / "settings.json"

    def _read(self) -> dict | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, UnicodeError, ValueError, RecursionError):
            raise SettingsStorageError("Model settings storage is unreadable.") from None
        if not _valid_stored_snapshot(payload):
            raise SettingsStorageError("Model settings storage has an invalid format.")
        return payload

    def _initial(self) -> dict:
        from models.model_pool import (
            DEFAULT_PROVIDER_SETTINGS, _json_mapping, _normalize_profile_name,
            _profile_from_mapping, _routes_from_env, build_model_pool_from_env,
        )
        env = self.environ
        try:
            initial_env = dict(env)
            initial_env.setdefault("LLM_PROVIDER", "deepseek")
            pool = build_model_pool_from_env(initial_env)
            return {
                "revision": 0, "default_profile": pool.default_profile,
                "profiles": [_profile_snapshot(profile) for profile in pool.profiles.values()],
                "routes": {purpose: list(chain) for purpose, chain in pool.routes.items()},
            }
        except (RuntimeError, ValueError, TypeError):
            pass
        selected = _normalize_profile_name(env.get("LLM_PROVIDER", "deepseek"))
        result = {"revision": 0, "default_profile": selected, "profiles": [], "routes": {}}
        try:
            raw_profiles = _json_mapping(env.get("LLM_PROVIDERS_JSON", ""), "LLM_PROVIDERS_JSON")
            candidates = raw_profiles or {name: {} for name in dict.fromkeys([selected, *DEFAULT_PROVIDER_SETTINGS])}
            for name, raw in candidates.items():
                if not isinstance(raw, dict):
                    continue
                try:
                    profile = _profile_from_mapping(name=name, raw=raw, env=env,
                                                    selected=selected, require_key=False)
                    if profile is None and (raw_profiles or name == selected):
                        # Show an editable definition even when its key is absent.
                        profile = _profile_from_mapping(name=name, raw={**raw, "api_key": "preview-only"},
                                                        env=env, selected=selected, require_key=True)
                        snapshot = _profile_snapshot(profile)
                        snapshot["api_key"] = ""
                    elif profile is not None:
                        snapshot = _profile_snapshot(profile)
                    else:
                        continue
                    result["profiles"].append(snapshot)
                except (RuntimeError, ValueError, TypeError):
                    continue
            names = {item["name"] for item in result["profiles"]}
            default = _normalize_profile_name(env.get("LLM_DEFAULT_PROVIDER") or env.get("LLM_ROUTE_DEFAULT") or selected)
            if raw_profiles and default not in names and names:
                default = result["profiles"][0]["name"]
            result["default_profile"] = default
            result["routes"] = {purpose: list(chain) for purpose, chain in _routes_from_env(env, default).items()}
        except (RuntimeError, ValueError, TypeError):
            pass
        return result

    def _public(self, snapshot: dict, *, inherited: bool = False) -> dict:
        from models.model_pool import DEFAULT_PROVIDER_SETTINGS, ROUTE_ENV_NAMES
        configured = True
        error = ""
        try:
            env = dict(self.environ) if inherited else _model_environment(self.environ, snapshot)
            _validated_pool(snapshot, env)
        except (RuntimeError, ValueError, TypeError, KeyError):
            configured = False
            error = "模型配置不完整或无效，请补充 API Key、接口地址和模型，并检查路由。"
        profiles = []
        for profile in snapshot["profiles"]:
            public = {key: value for key, value in profile.items() if key != "api_key"}
            public["has_api_key"] = bool(profile.get("api_key"))
            profiles.append(public)
        result = {
            "revision": snapshot["revision"], "configured": configured,
            "default_profile": snapshot["default_profile"], "profiles": profiles,
            "routes": snapshot["routes"],
            "provider_defaults": {name: dict(settings) for name, settings in DEFAULT_PROVIDER_SETTINGS.items()},
            "route_purposes": list(ROUTE_ENV_NAMES),
        }
        if error:
            result["error"] = error
        return result

    def get_public(self) -> dict:
        stored = self._read()
        return self._public(stored or self._initial(), inherited=stored is None)

    def effective_environ(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        env = self.environ if base is None else base
        snapshot = self._read()
        try:
            return _model_environment(env, snapshot) if snapshot else dict(env)
        except (KeyError, TypeError, ValueError):
            raise SettingsStorageError("Model settings storage has an invalid format.") from None

    @contextmanager
    def _locked(self):
        descriptor = None
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor = os.open(str(self.path) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        except OSError:
            raise SettingsStorageError("Model settings storage is unavailable.") from None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _write(self, snapshot: dict) -> None:
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".settings-", suffix=".json", dir=self.path.parent)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), 0o600)
                json.dump(snapshot, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        except OSError:
            raise SettingsStorageError("Model settings could not be saved.") from None
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass

    def update(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) - {"revision", "default_profile", "profiles", "routes"}:
            raise SettingsValidationError("Unknown or invalid model settings fields.")
        revision = payload.get("revision")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise SettingsValidationError("revision must be a nonnegative integer.")
        with self._locked():
            previous = self._read() or self._initial()
            if revision != previous["revision"]:
                raise SettingsConflictError("模型设置已被更新，请刷新后重试。")
            candidate = self._validate(payload, previous)
            candidate.update(schema_version=1, revision=revision + 1)
            self._write(candidate)
            return self._public(candidate)

    def _current_for_revision(self, revision: Any) -> dict:
        if type(revision) is not int or revision < 0:
            raise SettingsValidationError("revision must be a nonnegative integer.")
        previous = self._read() or self._initial()
        if revision != previous["revision"]:
            raise SettingsConflictError("模型设置已被更新，请刷新后重试。")
        return previous

    def validate_submission(self, payload: dict) -> dict:
        """Validate a draft using exactly the save rules, without writing files."""
        if not isinstance(payload, dict) or set(payload) - {"revision", "default_profile", "profiles", "routes"}:
            raise SettingsValidationError("Unknown or invalid model settings fields.")
        previous = self._current_for_revision(payload.get("revision"))
        return self._validate(payload, previous)

    def prepare_probe(self, payload: dict) -> dict:
        """Resolve a draft's credential only after its version and target match."""
        if not isinstance(payload, dict) or set(payload) - {"revision", "profile"}:
            raise SettingsValidationError("Unknown or invalid model probe fields.")
        previous = self._current_for_revision(payload.get("revision"))
        return self._clean_profile(payload.get("profile"), previous)

    def preview(self, payload: dict) -> dict:
        from models.model_pool import PURPOSE_ALIASES, ROUTE_ENV_NAMES
        candidate = self.validate_submission(payload)
        pool = _validated_pool(candidate)
        routes = []
        for purpose in dict.fromkeys([*ROUTE_ENV_NAMES, *pool.routes]):
            if purpose in pool.routes:
                source, source_purpose = "explicit", purpose
            elif PURPOSE_ALIASES.get(purpose) in pool.routes:
                source, source_purpose = "alias", PURPOSE_ALIASES[purpose]
            elif "default" in pool.routes:
                source, source_purpose = "default", "default"
            else:
                source, source_purpose = "default_profile", None
            raw_chain = pool.routes[source_purpose] if source_purpose else (pool.default_profile,)
            origins = {}
            for name in raw_chain:
                origins.setdefault(name, {"source": "route"})
                for fallback in pool.profiles[name].fallbacks:
                    origins.setdefault(fallback, {"source": "profile_fallback", "fallback_of": name})
            profiles = []
            for name in pool.route_profile_names(purpose):
                profile = pool.profile_named(name)
                profiles.append({"name": name, "provider": profile.provider, "model": profile.model, **origins[name]})
            routes.append({"purpose": purpose, "source": source, "source_purpose": source_purpose, "profiles": profiles})
        return {"revision": payload["revision"], "configured": True, "routes": routes}

    def _clean_profile(self, submitted: dict, previous: dict) -> dict:
        from models.model_pool import DEFAULT_PROVIDER_SETTINGS, ModelProfile
        previous_by_name = {profile["name"]: profile for profile in previous["profiles"]}
        allowed = set(ModelProfile.__dataclass_fields__) | {"has_api_key"}
        if not isinstance(submitted, dict) or set(submitted) - allowed:
            raise SettingsValidationError("Unknown or invalid profile fields.")
        name = _name(submitted.get("name"), "profile.name")
        provider = _name(submitted.get("provider") or name, "profile.provider")
        defaults = DEFAULT_PROVIDER_SETTINGS.get(provider, {})
        profile = {"name": name, "provider": provider}
        key = submitted.get("api_key", "")
        if not isinstance(key, str) or len(key) > 8192 or any(char in key for char in "\r\n\x00"):
            raise SettingsValidationError("profile.api_key must be a valid single-line string.")
        for field in ("base_url", "model", *_STRING_FIELDS):
            value = submitted.get(field, defaults.get(field, ""))
            if not isinstance(value, str) or len(value) > 2048 or any(char in value for char in "\r\n\x00"):
                raise SettingsValidationError(f"profile.{field} must be a valid single-line string.")
            profile[field] = value.strip()
        try:
            url = urlsplit(profile["base_url"])
            if (url.scheme not in {"http", "https"} or not url.hostname
                    or url.username is not None or url.password is not None
                    or url.query or url.fragment):
                raise ValueError()
            if url.port == 0:
                raise ValueError()
        except ValueError:
            raise SettingsValidationError("profile.base_url must be an HTTP(S) URL without credentials, a query or a fragment.") from None
        previous_profile = previous_by_name.get(name)
        retained_key = ""
        try:
            if previous_profile and _target_identity(profile) == _target_identity(previous_profile):
                retained_key = previous_profile.get("api_key", "")
        except (ValueError, UnicodeError):
            pass
        profile["api_key"] = key.strip() or retained_key
        if not profile["api_key"]:
            raise SettingsValidationError("填写 API Key；仅配置名称、Provider 和接口地址均未改变时可留空保留原 Key。")
        if not profile["model"]:
            raise SettingsValidationError("profile.model is required.")
        if not profile["max_tokens_param"]:
            profile["max_tokens_param"] = str(defaults.get("max_tokens_param", "max_tokens"))
        wire_api = submitted.get("wire_api", "chat_completions")
        if not isinstance(wire_api, str) or wire_api not in {"chat_completions", "responses"}:
            raise SettingsValidationError("profile.wire_api must be chat_completions or responses.")
        profile["wire_api"] = wire_api
        for field in _TOKEN_FIELDS:
            value = submitted.get(field)
            if value in (None, ""):
                profile[field] = None
            elif isinstance(value, int) and not isinstance(value, bool) and 0 < value <= 100_000_000:
                profile[field] = value
            else:
                raise SettingsValidationError(f"profile.{field} must be a positive integer or null.")
        for field in _BOOLEAN_FIELDS:
            value = submitted.get(field, False)
            if not isinstance(value, bool):
                raise SettingsValidationError(f"profile.{field} must be a boolean.")
            profile[field] = value
        if profile["supports_thinking"] and not profile["thinking_param"]:
            raise SettingsValidationError("profile.thinking_param is required when supports_thinking is enabled.")
        profile["thinking_value"] = submitted.get("thinking_value", True)
        try:
            json.dumps(profile["thinking_value"], allow_nan=False)
        except (TypeError, ValueError, RecursionError):
            raise SettingsValidationError("profile.thinking_value must be a valid JSON value.") from None
        fallback = submitted.get("fallbacks", [])
        if not isinstance(fallback, list):
            raise SettingsValidationError("profile.fallbacks must be a list of profiles.")
        profile["fallbacks"] = _chain(fallback, "profile.fallbacks") if fallback else []
        return profile

    def _validate(self, payload: dict, previous: dict) -> dict:
        profiles = payload.get("profiles")
        if not isinstance(profiles, list) or not profiles or len(profiles) > 100:
            raise SettingsValidationError("profiles must contain between 1 and 100 model profiles.")
        cleaned = []
        names = set()
        for submitted in profiles:
            profile = self._clean_profile(submitted, previous)
            if profile["name"] in names:
                raise SettingsValidationError("Profile names must be unique.")
            names.add(profile["name"])
            cleaned.append(profile)
        default = _name(payload.get("default_profile"), "default_profile")
        if default not in names:
            raise SettingsValidationError("default_profile must reference an existing profile.")
        routes = payload.get("routes", {})
        if not isinstance(routes, dict) or len(routes) > 100:
            raise SettingsValidationError("routes must be an object with at most 100 purposes.")
        cleaned_routes = {}
        for purpose, chain in routes.items():
            normalized = _name(purpose, "route purpose")
            if normalized in cleaned_routes:
                raise SettingsValidationError("Route purposes must be unique.")
            cleaned_routes[normalized] = _chain(chain, "route profiles")
        cleaned_routes.setdefault("default", [default])
        for chain in list(cleaned_routes.values()) + [profile["fallbacks"] for profile in cleaned]:
            if any(name not in names for name in chain):
                raise SettingsValidationError("Routes and fallbacks must reference existing profiles.")
        candidate = {"default_profile": default, "profiles": cleaned, "routes": cleaned_routes}
        try:
            # An isolated env prevents discarded legacy advanced values from leaking
            # into the saved snapshot. Construction makes no network requests.
            pool = _validated_pool(candidate)
            candidate["profiles"] = [_profile_snapshot(profile) for profile in pool.profiles.values()]
        except (RuntimeError, ValueError, TypeError, KeyError):
            raise SettingsValidationError("Model settings are invalid; check profile capabilities and routes.") from None
        return candidate


def effective_environ(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return an independent effective snapshot; never mutate os.environ."""
    return RuntimeSettingsStore(environ=base).effective_environ()
