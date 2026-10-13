import unittest
from unittest.mock import patch

from runtime.context.dynamic_budget import reduce_prompt_to_hard_limit
from runtime.token_estimator import (
    estimate_tokens,
    output_tokens_for_call,
    safe_context_limit,
)


class CountingProvider:
    context_limit = 100

    def count_tokens(self, messages):
        return {"total_tokens": sum(len(str(message.get("content") or "")) for message in messages)}


class TokenEstimatorTests(unittest.TestCase):
    def test_provider_count_tokens_is_preferred(self) -> None:
        provider = CountingProvider()
        messages = [{"role": "user", "content": "12345"}]

        self.assertEqual(5, estimate_tokens(messages, provider=provider))
        self.assertEqual(85, safe_context_limit(provider))

    def test_safe_context_limit_reserves_output_budget(self) -> None:
        provider = CountingProvider()

        self.assertEqual(
            75,
            safe_context_limit(provider, reserved_output_tokens=10),
        )

    def test_output_tokens_for_call_is_clamped_to_remaining_context(self) -> None:
        provider = CountingProvider()

        self.assertEqual(
            15,
            output_tokens_for_call(
                provider,
                requested_output_tokens=80,
                input_tokens=70,
            ),
        )

    def test_fallback_estimator_counts_cjk_more_conservatively(self) -> None:
        text = "你好世界" * 10

        estimated = estimate_tokens([{"role": "user", "content": text}])

        self.assertGreaterEqual(estimated, len(text))

    def test_bpe_tokenizer_requires_explicit_enable_flag(self) -> None:
        class Provider:
            tokenizer_model = "gpt-4o"
            bpe_tokenizer_enabled = False

        with patch("runtime.token_estimator._count_with_tiktoken", return_value=1) as counter:
            estimated = estimate_tokens(
                [{"role": "user", "content": "你好世界" * 10}],
                provider=Provider(),
            )

        counter.assert_not_called()
        self.assertGreater(estimated, 1)

    def test_bpe_tokenizer_can_be_enabled_explicitly(self) -> None:
        class Provider:
            tokenizer_model = "gpt-4o"
            bpe_tokenizer_enabled = True

        with patch("runtime.token_estimator._count_with_tiktoken", return_value=7) as counter:
            estimated = estimate_tokens(
                [{"role": "user", "content": "hello"}],
                provider=Provider(),
            )

        counter.assert_called_once()
        self.assertEqual(7, estimated)

    def test_hard_reducer_keeps_or_drops_complete_tool_transactions(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first" * 300},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "second"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "x" * 500},
            {"role": "user", "content": "latest request"},
        ]
        provider = CountingProvider()
        for limit, expected_ids in ((120, set()), (600, {"call-1"})):
            with self.subTest(limit=limit):
                trimmed = reduce_prompt_to_hard_limit(messages, max_tokens=limit, provider=provider)
                self.assertEqual("system", trimmed[0]["content"])
                self.assertEqual("latest request", trimmed[-1]["content"])
                self.assertLessEqual(estimate_tokens(trimmed, provider=provider), limit)
                tool_call_ids = {
                    call["id"]
                    for message in trimmed
                    for call in (message.get("tool_calls") or [])
                }
                tool_results = [message for message in trimmed if message.get("role") == "tool"]
                self.assertEqual(expected_ids, tool_call_ids)
                self.assertEqual(expected_ids, {message["tool_call_id"] for message in tool_results})
                for result in tool_results:
                    self.assertEqual("x" * 500, result["content"])


if __name__ == "__main__":
    unittest.main()
