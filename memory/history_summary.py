from runtime.agent_spec import AgentSpec
from models.model_task_runner import ModelTaskRunner


class HistorySummarizer:
    """Compact assistant replies before they enter derived memory files."""

    def __init__(
        self,
        provider=None,
        model: str = "",
        runner: ModelTaskRunner | None = None,
        spec: AgentSpec | None = None,
        direct_limit: int = 240,
        summary_limit: int = 480,
        max_tokens: int = 220,
    ):
        self.provider = provider
        self.model = model
        self.runner = runner
        if self.runner is None and provider is not None and model:
            self.runner = ModelTaskRunner(
                provider=provider,
                model=model,
                default_max_tokens=max_tokens,
            )
        self.spec = spec or AgentSpec(
            name="history_summarizer",
            profile=None,
            model_purpose="summary",
            max_tokens=max_tokens,
        )
        self.direct_limit = direct_limit
        self.summary_limit = summary_limit
        self.max_tokens = max_tokens

    def summarize(self, assistant_text: str) -> str:
        text = assistant_text.strip()
        if not text:
            return ""
        if len(text) <= self.direct_limit or self.runner is None:
            return self._fallback(text)

        try:
            summary = self.runner.run(
                spec=self.spec,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Summarize the assistant reply for long-term memory. "
                            "Keep decisions, conclusions, file paths, commands, and "
                            "unresolved issues. Remove conversational filler. "
                            "Return only a compact plain-text summary."
                        ),
                    },
                    {"role": "user", "content": text},
                ],
                max_tokens=self.max_tokens,
            )
            summary = summary.strip()
            if summary:
                return self._trim(summary)
        except Exception:
            pass

        return self._fallback(text)

    def _fallback(self, text: str) -> str:
        return self._trim(text)

    def _trim(self, text: str) -> str:
        if len(text) <= self.summary_limit:
            return text
        return text[: self.summary_limit].rstrip() + "..."
