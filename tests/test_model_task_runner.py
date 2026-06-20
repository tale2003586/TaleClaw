import unittest

from runtime.agent_spec import AgentSpec
from models.model_task_runner import ModelTaskRunner
from models.provider import LLMResponse
from memory.history_summary import HistorySummarizer


class RecordingProvider:
    def __init__(self, content: str = "summary") -> None:
        self.content = content
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(content=self.content)


class FailingProvider:
    def chat(self, **kwargs):
        raise RuntimeError("model unavailable")


class FakeModelPool:
    def __init__(self, provider):
        self.provider = provider
        self.purposes = []

    def routed_provider(self, purpose: str):
        self.purposes.append(("provider", purpose))
        return self.provider

    def model_for(self, purpose: str):
        self.purposes.append(("model", purpose))
        return f"{purpose}-model"


class ModelTaskRunnerTests(unittest.TestCase):
    def test_runner_routes_one_shot_model_task_by_spec(self):
        provider = RecordingProvider("task output")
        pool = FakeModelPool(provider)
        runner = ModelTaskRunner(model_pool=pool)
        spec = AgentSpec(
            name="history_summarizer",
            profile=None,
            model_purpose="summary",
            max_tokens=123,
        )

        output = runner.run(
            spec=spec,
            messages=[{"role": "user", "content": "summarize this"}],
        )

        self.assertEqual("task output", output)
        self.assertEqual("summary-model", provider.calls[0]["model"])
        self.assertEqual(123, provider.calls[0]["max_tokens"])
        self.assertEqual([], provider.calls[0]["tools"])
        self.assertEqual("none", provider.calls[0]["tool_choice"])
        self.assertIn(("provider", "summary"), pool.purposes)
        self.assertIn(("model", "summary"), pool.purposes)

    def test_history_summarizer_uses_model_task_runner(self):
        provider = RecordingProvider("compact memory summary")
        runner = ModelTaskRunner(provider=provider, model="summary-model")
        summarizer = HistorySummarizer(
            runner=runner,
            spec=AgentSpec(
                name="history_summarizer",
                profile=None,
                model_purpose="summary",
                max_tokens=77,
            ),
            direct_limit=5,
            max_tokens=77,
        )

        summary = summarizer.summarize("this is a long assistant answer")

        self.assertEqual("compact memory summary", summary)
        self.assertEqual("summary-model", provider.calls[0]["model"])
        self.assertEqual(77, provider.calls[0]["max_tokens"])

    def test_runner_calls_on_error_before_reraising(self):
        errors = []
        runner = ModelTaskRunner(
            provider=FailingProvider(),
            model="summary-model",
            on_error=lambda exc, spec: errors.append((type(exc).__name__, spec.name)),
        )
        spec = AgentSpec(
            name="candidate_extractor",
            profile=None,
            model_purpose="summary",
        )

        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            runner.run(spec=spec, messages=[{"role": "user", "content": "x"}])

        self.assertEqual([("RuntimeError", "candidate_extractor")], errors)


if __name__ == "__main__":
    unittest.main()
