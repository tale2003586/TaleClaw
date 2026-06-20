import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from openai import OpenAI

from models.provider import LLMResponse, OpenAICompatibleProvider


DEFAULT_PROVIDER_SETTINGS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-v4-flash",
        "max_tokens_param": "max_tokens",
    },
    "mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "model": "mimo-v2.5-pro",
        "max_tokens_param": "max_completion_tokens",
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4.1-mini",
        "max_tokens_param": "max_tokens",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "model": "gemini-3.5-flash",
        "max_tokens_param": "max_tokens",
    },
}

PURPOSE_ALIASES = {
    "teammate": "coding",
    "reflection": "summary",
    "task_conclusion": "summary",
    "compact": "summary",
}

ROUTE_ENV_NAMES = {
    "default": "LLM_ROUTE_DEFAULT",
    "chat": "LLM_ROUTE_CHAT",
    "coding": "LLM_ROUTE_CODING",
    "summary": "LLM_ROUTE_SUMMARY",
    "hybrid": "LLM_ROUTE_HYBRID",
    "compact": "LLM_ROUTE_COMPACT",
    "teammate": "LLM_ROUTE_TEAMMATE",
    "reflection": "LLM_ROUTE_REFLECTION",
    "task_conclusion": "LLM_ROUTE_TASK_CONCLUSION",
}


@dataclass(frozen=True)
class ModelProfile:
    name: str
    provider: str
    api_key: str = field(repr=False)
    base_url: str
    model: str
    max_tokens_param: str = "max_tokens"
    wire_api: str = "chat_completions"
    fallbacks: tuple[str, ...] = ()


class ModelRouteError(RuntimeError):
    def __init__(
        self,
        *,
        purpose: str,
        attempts: list[dict[str, Any]],
        message: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.purpose = purpose
        self.attempts = attempts
        self.__cause__ = cause


@dataclass
class ModelProfileHealth:
    consecutive_failures: int = 0
    disabled_until: float = 0.0
    last_error: str = ""
    last_checked_at: float = 0.0


class ModelPool:
    def __init__(
        self,
        *,
        profiles: Mapping[str, ModelProfile],
        routes: Mapping[str, tuple[str, ...]] | None = None,
        default_profile: str,
        client_factory: Callable[..., Any] = OpenAI,
        failure_threshold: int = 3,
        failure_cooldown_seconds: float = 300.0,
    ) -> None:
        if not profiles:
            raise RuntimeError("No model providers are configured.")
        if default_profile not in profiles:
            raise RuntimeError(f"Default model provider is not configured: {default_profile}")
        self.profiles = dict(profiles)
        self.routes = dict(routes or {})
        self.default_profile = default_profile
        self.client_factory = client_factory
        self.failure_threshold = max(1, int(failure_threshold))
        self.failure_cooldown_seconds = max(0.0, float(failure_cooldown_seconds))
        self._clients: dict[str, Any] = {}
        self._providers: dict[str, OpenAICompatibleProvider] = {}
        self._routed: dict[str, RoutedModelProvider] = {}
        self._health: dict[str, ModelProfileHealth] = {}

    def primary_profile_name(self, purpose: str = "chat") -> str:
        return self.route_profile_names(purpose)[0]

    def profile_for(self, purpose: str = "chat") -> ModelProfile:
        return self.route_profiles_for_call(purpose)[0]

    def model_for(self, purpose: str = "chat") -> str:
        return self.profile_for(purpose).model

    def client_for_profile(self, profile_name: str) -> Any:
        profile = self._require_profile(profile_name)
        if profile.name not in self._clients:
            self._clients[profile.name] = self.client_factory(
                api_key=profile.api_key,
                base_url=profile.base_url,
            )
        return self._clients[profile.name]

    def client_for_purpose(self, purpose: str = "chat") -> Any:
        return self.client_for_profile(self.profile_for(purpose).name)

    def provider_for_profile(self, profile_name: str) -> OpenAICompatibleProvider:
        profile = self._require_profile(profile_name)
        if profile.name not in self._providers:
            self._providers[profile.name] = OpenAICompatibleProvider(
                self.client_for_profile(profile.name),
                max_tokens_param=profile.max_tokens_param,
                wire_api=profile.wire_api,
            )
        return self._providers[profile.name]

    def routed_provider(self, purpose: str = "chat") -> "RoutedModelProvider":
        key = _normalize_route_name(purpose)
        if key not in self._routed:
            self._routed[key] = RoutedModelProvider(self, key)
        return self._routed[key]

    def route_profiles(self, purpose: str = "chat") -> list[ModelProfile]:
        return [self.profiles[name] for name in self.route_profile_names(purpose)]

    def route_profiles_for_call(self, purpose: str = "chat") -> list[ModelProfile]:
        chain = self.route_profiles(purpose)
        available = [profile for profile in chain if self.profile_available(profile.name)]
        return available or chain

    def route_profile_names(self, purpose: str = "chat") -> list[str]:
        names = self._route_chain_for(purpose)
        chain: list[str] = []
        for name in names:
            if name not in self.profiles:
                raise RuntimeError(
                    f"Model route '{purpose}' references unknown provider '{name}'."
                )
            if name not in chain:
                chain.append(name)
            for fallback in self.profiles[name].fallbacks:
                if fallback not in self.profiles:
                    raise RuntimeError(
                        f"Model provider '{name}' references unknown fallback '{fallback}'."
                    )
                if fallback not in chain:
                    chain.append(fallback)
        return chain

    def _route_chain_for(self, purpose: str) -> tuple[str, ...]:
        key = _normalize_route_name(purpose)
        if key in self.routes:
            return self.routes[key]
        alias = PURPOSE_ALIASES.get(key)
        if alias and alias in self.routes:
            return self.routes[alias]
        if "default" in self.routes:
            return self.routes["default"]
        return (self.default_profile,)

    def _require_profile(self, profile_name: str) -> ModelProfile:
        if profile_name not in self.profiles:
            raise RuntimeError(f"Model provider is not configured: {profile_name}")
        return self.profiles[profile_name]

    def profile_available(self, profile_name: str) -> bool:
        health = self._health.get(profile_name)
        if health is None:
            return True
        return time.time() >= health.disabled_until

    def profile_health(self, profile_name: str) -> dict[str, Any]:
        self._require_profile(profile_name)
        health = self._health.get(profile_name, ModelProfileHealth())
        return {
            "profile": profile_name,
            "available": self.profile_available(profile_name),
            "consecutive_failures": health.consecutive_failures,
            "disabled_until": health.disabled_until,
            "last_error": health.last_error,
            "last_checked_at": health.last_checked_at,
        }

    def mark_profile_success(self, profile_name: str) -> None:
        self._health[profile_name] = ModelProfileHealth(
            consecutive_failures=0,
            disabled_until=0.0,
            last_error="",
            last_checked_at=time.time(),
        )

    def mark_profile_failure(
        self,
        profile_name: str,
        exc: Exception,
        *,
        force_disable: bool = False,
    ) -> None:
        now = time.time()
        health = self._health.get(profile_name, ModelProfileHealth())
        failures = health.consecutive_failures + 1
        disabled_until = health.disabled_until
        if force_disable or failures >= self.failure_threshold:
            disabled_until = now + self.failure_cooldown_seconds
        self._health[profile_name] = ModelProfileHealth(
            consecutive_failures=failures,
            disabled_until=disabled_until,
            last_error=f"{type(exc).__name__}: {exc}",
            last_checked_at=now,
        )

    def health_check_profile(
        self,
        profile_name: str,
        *,
        max_tokens: int = 8,
    ) -> dict[str, Any]:
        profile = self._require_profile(profile_name)
        provider = self.provider_for_profile(profile.name)
        started = time.perf_counter()
        try:
            provider.chat(
                model=profile.model,
                messages=[{"role": "user", "content": "ping"}],
                tools=[],
                tool_choice="none",
                max_tokens=max_tokens,
            )
        except Exception as exc:
            self.mark_profile_failure(profile.name, exc, force_disable=True)
            return {
                **self.profile_health(profile.name),
                "status": "failed",
                "duration_ms": _elapsed_ms(started),
            }
        self.mark_profile_success(profile.name)
        return {
            **self.profile_health(profile.name),
            "status": "success",
            "duration_ms": _elapsed_ms(started),
        }

    def health_check_purposes(
        self,
        purposes: list[str] | tuple[str, ...],
        *,
        max_tokens: int = 8,
    ) -> list[dict[str, Any]]:
        checked: set[str] = set()
        results = []
        for purpose in purposes:
            names = self.route_profile_names(purpose)
            if not names:
                continue
            name = names[0]
            if name in checked:
                continue
            checked.add(name)
            result = self.health_check_profile(name, max_tokens=max_tokens)
            result["purpose"] = purpose
            results.append(result)
        return results


class RoutedModelProvider:
    def __init__(self, pool: ModelPool, purpose: str) -> None:
        self.pool = pool
        self.purpose = purpose

    def chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        tool_choice: str = "auto",
    ) -> LLMResponse:
        return self._call_with_fallbacks(
            stream=False,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
        )

    def stream_chat(
        self,
        *,
        messages: list[dict],
        tools: list[dict],
        model: str,
        max_tokens: int,
        on_text: Callable[[str], None],
        tool_choice: str = "auto",
    ) -> LLMResponse:
        return self._call_with_fallbacks(
            stream=True,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            tool_choice=tool_choice,
            on_text=on_text,
        )

    def _call_with_fallbacks(self, *, stream: bool, **kwargs) -> LLMResponse:
        chain = self.pool.route_profiles_for_call(self.purpose)
        if not chain:
            raise RuntimeError(f"No model route configured for purpose '{self.purpose}'.")

        errors: list[str] = []
        attempts: list[dict[str, Any]] = []
        last_error: Exception | None = None
        configured_primary_name = self.pool.route_profile_names(self.purpose)[0]

        for profile in chain:
            provider = self.pool.provider_for_profile(profile.name)
            call_kwargs = dict(kwargs)
            selected_model = (
                str(kwargs.get("model") or "").strip()
                if profile.name == configured_primary_name and kwargs.get("model")
                else profile.model
            )
            call_kwargs["model"] = selected_model
            emitted = False
            try:
                if stream:
                    user_on_text = call_kwargs.pop("on_text")

                    def track_text(text: str) -> None:
                        nonlocal emitted
                        if text:
                            emitted = True
                        user_on_text(text)

                    call_kwargs["on_text"] = track_text
                    response = provider.stream_chat(**call_kwargs)
                else:
                    response = provider.chat(**call_kwargs)
                attempts.append({
                    "profile": profile.name,
                    "provider": profile.provider,
                    "model": selected_model,
                    "status": "success",
                })
                self.pool.mark_profile_success(profile.name)
                response.provider_metadata = {
                    **(response.provider_metadata or {}),
                    "route_purpose": self.purpose,
                    "selected_profile": profile.name,
                    "selected_provider": profile.provider,
                    "selected_model": selected_model,
                    "retry_count": max(0, len(attempts) - 1),
                    "attempts": attempts,
                }
                return response
            except Exception as exc:
                self.pool.mark_profile_failure(profile.name, exc)
                if stream and emitted:
                    raise
                last_error = exc
                error = f"{type(exc).__name__}: {exc}"
                attempts.append({
                    "profile": profile.name,
                    "provider": profile.provider,
                    "model": selected_model,
                    "status": "failed",
                    "error": error,
                })
                errors.append(f"{profile.name}: {error}")

        detail = "; ".join(errors) or "no attempts"
        raise ModelRouteError(
            purpose=self.purpose,
            attempts=attempts,
            message=f"All model providers failed for purpose '{self.purpose}': {detail}",
            cause=last_error,
        )


def build_model_pool_from_env(
    environ: Mapping[str, str] | None = None,
    *,
    client_factory: Callable[..., Any] = OpenAI,
) -> ModelPool:
    env = environ or os.environ
    selected = _normalize_profile_name(env.get("LLM_PROVIDER", "deepseek"))

    profiles, default_profile = _profiles_from_env(env, selected)
    routes = _routes_from_env(env, default_profile)
    return ModelPool(
        profiles=profiles,
        routes=routes,
        default_profile=default_profile,
        client_factory=client_factory,
        failure_threshold=_int_env(env, "LLM_PROVIDER_FAILURE_THRESHOLD", 3),
        failure_cooldown_seconds=_float_env(
            env,
            "LLM_PROVIDER_FAILURE_COOLDOWN_SECONDS",
            300.0,
        ),
    )


def _profiles_from_env(
    env: Mapping[str, str],
    selected: str,
) -> tuple[dict[str, ModelProfile], str]:
    raw_profiles = _json_mapping(env.get("LLM_PROVIDERS_JSON", ""), "LLM_PROVIDERS_JSON")
    if raw_profiles:
        profiles = {}
        for name, raw_profile in raw_profiles.items():
            if not isinstance(raw_profile, dict):
                raise RuntimeError(f"LLM_PROVIDERS_JSON.{name} must be an object.")
            profile = _profile_from_mapping(
                name=name,
                raw=raw_profile,
                env=env,
                selected=selected,
                require_key=True,
            )
            profiles[profile.name] = profile
        default_profile = _normalize_profile_name(
            env.get("LLM_DEFAULT_PROVIDER")
            or env.get("LLM_ROUTE_DEFAULT")
            or selected
        )
        if default_profile not in profiles:
            default_profile = next(iter(profiles))
        return profiles, default_profile

    profiles: dict[str, ModelProfile] = {}
    selected_profile = _profile_from_mapping(
        name=selected,
        raw={},
        env=env,
        selected=selected,
        require_key=True,
    )
    profiles[selected_profile.name] = selected_profile

    for name in DEFAULT_PROVIDER_SETTINGS:
        normalized = _normalize_profile_name(name)
        if normalized in profiles:
            continue
        profile = _profile_from_mapping(
            name=normalized,
            raw={},
            env=env,
            selected=selected,
            require_key=False,
        )
        if profile is not None:
            profiles[profile.name] = profile
    return profiles, selected_profile.name


def _profile_from_mapping(
    *,
    name: str,
    raw: Mapping[str, Any],
    env: Mapping[str, str],
    selected: str,
    require_key: bool,
) -> ModelProfile | None:
    profile_name = _normalize_profile_name(name)
    provider = _normalize_profile_name(str(raw.get("provider") or profile_name))
    default_settings = DEFAULT_PROVIDER_SETTINGS.get(provider, {})
    selected_match = profile_name == selected or provider == selected

    api_key = str(raw.get("api_key") or "").strip()
    if not api_key:
        api_key = _first_env(
            env,
            _api_key_env_names(
                profile_name=profile_name,
                provider=provider,
                selected_match=selected_match,
                explicit=raw.get("api_key_env"),
            ),
        )
    if not api_key:
        if not require_key:
            return None
        joined = " or ".join(
            _api_key_env_names(
                profile_name=profile_name,
                provider=provider,
                selected_match=selected_match,
                explicit=raw.get("api_key_env"),
            )
        )
        raise RuntimeError(f"Missing model API key for provider '{profile_name}'. Set {joined}.")

    base_url = (
        str(raw.get("base_url") or "").strip()
        or _first_env(
            env,
            _provider_env_names(
                profile_name,
                provider,
                selected_match=selected_match,
                suffix="BASE_URL",
            ),
        )
        or str(default_settings.get("base_url") or "").strip()
    )
    if not base_url:
        raise RuntimeError(
            f"Missing base URL for model provider '{profile_name}'. Set LLM_BASE_URL "
            f"or {_env_prefix(profile_name)}_BASE_URL."
        )

    model = (
        str(raw.get("model") or "").strip()
        or _first_env(
            env,
            _provider_env_names(
                profile_name,
                provider,
                selected_match=selected_match,
                suffix="MODEL",
            ),
        )
        or str(default_settings.get("model") or "").strip()
    )
    if not model:
        raise RuntimeError(
            f"Missing model name for model provider '{profile_name}'. Set LLM_MODEL "
            f"or {_env_prefix(profile_name)}_MODEL."
        )

    max_tokens_param = (
        str(raw.get("max_tokens_param") or "").strip()
        or _first_env(
            env,
            _provider_env_names(
                profile_name,
                provider,
                selected_match=selected_match,
                suffix="MAX_TOKENS_PARAM",
            ),
        )
        or str(default_settings.get("max_tokens_param") or "max_tokens").strip()
    )
    wire_api = _normalize_wire_api(
        str(raw.get("wire_api") or "").strip()
        or _first_env(
            env,
            _provider_env_names(
                profile_name,
                provider,
                selected_match=selected_match,
                suffix="WIRE_API",
            ),
        )
        or str(default_settings.get("wire_api") or "chat_completions").strip()
    )
    fallbacks = _parse_profile_list(raw.get("fallbacks", ()))

    return ModelProfile(
        name=profile_name,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
        max_tokens_param=max_tokens_param,
        wire_api=wire_api,
        fallbacks=fallbacks,
    )


def _routes_from_env(
    env: Mapping[str, str],
    default_profile: str,
) -> dict[str, tuple[str, ...]]:
    routes: dict[str, tuple[str, ...]] = {
        "default": (_normalize_profile_name(default_profile),),
    }
    raw_routes = _json_mapping(env.get("LLM_ROUTES_JSON", ""), "LLM_ROUTES_JSON")
    for purpose, value in raw_routes.items():
        routes[_normalize_route_name(purpose)] = _parse_profile_list(value)

    for purpose, env_name in ROUTE_ENV_NAMES.items():
        value = str(env.get(env_name, "")).strip()
        if value:
            routes[purpose] = _parse_profile_list(value)

    fallback_chain = _parse_profile_list(env.get("LLM_ROUTE_FALLBACK", ""))
    if fallback_chain:
        for purpose, chain in list(routes.items()):
            routes[purpose] = _merge_profile_chains(chain, fallback_chain)
    return routes


def _api_key_env_names(
    *,
    profile_name: str,
    provider: str,
    selected_match: bool,
    explicit: Any,
) -> list[str]:
    names = []
    if explicit:
        names.extend(_parse_env_name_list(explicit))
    if selected_match:
        names.append("LLM_API_KEY")
    names.append(f"{_env_prefix(profile_name)}_API_KEY")
    names.append(f"{_env_prefix(provider)}_API_KEY")
    return _dedupe(names)


def _provider_env_names(
    profile_name: str,
    provider: str,
    *,
    selected_match: bool,
    suffix: str,
) -> list[str]:
    names = []
    if selected_match:
        names.append(f"LLM_{suffix}")
    names.append(f"{_env_prefix(profile_name)}_{suffix}")
    names.append(f"{_env_prefix(provider)}_{suffix}")
    return _dedupe(names)


def _json_mapping(value: str, env_name: str) -> dict[str, Any]:
    text = str(value or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{env_name} must be valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{env_name} must be a JSON object.")
    return payload


def _parse_profile_list(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        items = re.split(r"[,\s]+", value.strip()) if value.strip() else []
    elif isinstance(value, (list, tuple)):
        items = value
    else:
        raise RuntimeError("Model route values must be strings or lists.")
    return tuple(
        _normalize_profile_name(str(item))
        for item in items
        if str(item).strip()
    )


def _parse_env_name_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [item.strip() for item in re.split(r"[,\s]+", value) if item.strip()]
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    raise RuntimeError("api_key_env must be a string or a list.")


def _first_env(env: Mapping[str, str], names: list[str]) -> str:
    for name in names:
        value = str(env.get(name, "")).strip()
        if value:
            return value
    return ""


def _merge_profile_chains(
    primary: tuple[str, ...],
    fallback: tuple[str, ...],
) -> tuple[str, ...]:
    result = list(primary)
    for item in fallback:
        if item not in result:
            result.append(item)
    return tuple(result)


def _normalize_profile_name(value: str) -> str:
    return str(value or "").strip().lower().replace("-", "_")


def _normalize_route_name(value: str) -> str:
    return _normalize_profile_name(value)


def _normalize_wire_api(value: str) -> str:
    normalized = str(value or "chat_completions").strip().lower().replace("-", "_")
    if normalized in {"chat", "chat_completion", "chat_completions"}:
        return "chat_completions"
    if normalized in {"response", "responses"}:
        return "responses"
    raise RuntimeError(
        "Model provider wire_api must be 'chat_completions' or 'responses'."
    )


def _env_prefix(value: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", str(value).upper()).strip("_")


def _dedupe(items: list[str]) -> list[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _int_env(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(str(env.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _float_env(env: Mapping[str, str], name: str, default: float) -> float:
    try:
        return float(str(env.get(name, "") or default).strip())
    except (TypeError, ValueError):
        return default


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 3)
