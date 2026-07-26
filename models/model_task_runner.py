from dataclasses import dataclass
from time import monotonic
from typing import Any

from runtime.agent_spec import AgentSpec


@dataclass(frozen=True)
class ModelTaskResult:
    content: str
    usage: Any = None
    provider_metadata: dict[str, Any] | None = None
    elapsed_ms: float = 0


class ModelTaskRunner:
    """Run one-shot model tasks that do not need tools or session lifecycle."""

    def __init__(
        self,
        *,
        provider=None,
        model: str = "",
        model_pool=None,
        default_max_tokens: int = 800,
        on_error=None,
    ) -> None:
        self.provider = provider
        self.model = model
        self.model_pool = model_pool
        self.default_max_tokens = max(1, int(default_max_tokens))
        self.on_error = on_error

    def run(
        self,
        *,
        spec: AgentSpec,
        messages: list[dict],
        max_tokens: int | None = None,
    ) -> str:
        return self.run_result(
            spec=spec,
            messages=messages,
            max_tokens=max_tokens,
        ).content

    def run_result(
        self,
        *,
        spec: AgentSpec,
        messages: list[dict],
        max_tokens: int | None = None,
    ) -> ModelTaskResult:
        provider, model = self._provider_and_model(spec)
        started = monotonic()
        try:
            response = provider.chat(
                model=model,
                messages=messages,
                tools=[],
                tool_choice="none",
                max_tokens=max(1, int(max_tokens or spec.max_tokens or self.default_max_tokens)),
            )
        except Exception as exc:
            if self.on_error is not None:
                self.on_error(exc, spec)
            raise
        return ModelTaskResult(
            content=str(response.content or ""),
            usage=getattr(response, "usage", None),
            provider_metadata=dict(getattr(response, "provider_metadata", {}) or {}),
            elapsed_ms=(monotonic() - started) * 1000,
        )

    def _provider_and_model(self, spec: AgentSpec):
        if self.model_pool is not None:
            purpose = spec.model_purpose or "summary"
            return (
                self.model_pool.routed_provider(purpose),
                self.model_pool.model_for(purpose),
            )
        if self.provider is None:
            raise RuntimeError("ModelTaskRunner has no provider or model_pool.")
        return self.provider, self.model
