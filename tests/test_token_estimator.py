import unittest
from unittest.mock import patch

from runtime.token_estimator import (
    emergency_trim,
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

    def test_emergency_trim_preserves_tool_call_pairing(self) -> None:
        messages = [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "first"},
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

        trimmed = emergency_trim(messages, max_tokens=120)

        self.assertEqual("system", trimmed[0]["content"])
        tool_call_ids = {
            call["id"]
            for message in trimmed
            for call in (message.get("tool_calls") or [])
        }
        tool_result_ids = {
            message.get("tool_call_id")
            for message in trimmed
            if message.get("role") == "tool"
        }
        self.assertTrue(tool_result_ids <= tool_call_ids)

    def test_emergency_trim_can_shrink_single_large_message(self) -> None:
        provider = CountingProvider()
        trimmed = emergency_trim(
            [{"role": "user", "content": "x" * 500}],
            max_tokens=100,
            provider=provider,
        )

        self.assertLessEqual(estimate_tokens(trimmed, provider=provider), 100)
        self.assertIn("emergency trimmed", trimmed[0]["content"])


if __name__ == "__main__":
    unittest.main()
