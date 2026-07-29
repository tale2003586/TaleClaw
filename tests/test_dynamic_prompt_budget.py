import unittest
from types import SimpleNamespace

from applications.coding.context_state import (
    CODING_CONTEXT_STATE_METADATA_KEY,
    build_coding_context_view,
)
from runtime.context import ContextBuilder
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS

from runtime.context.dynamic_budget import (
    PromptBudgetExceeded,
    calculate_dynamic_prompt_budget,
    enforce_hard_token_guard,
    select_complete_groups,
)
from runtime.sessions.session import Session


class _Provider:
    def __init__(self, window: int) -> None:
        self.context_window_tokens = window


class DynamicPromptBudgetTests(unittest.TestCase):
    def test_budget_changes_with_model_window_and_fixed_prompt_costs(self) -> None:
        system = [{"role": "system", "content": "rules " * 100}]
        tools = [{"type": "function", "function": {"name": "read", "parameters": {}}}]
        small = calculate_dynamic_prompt_budget(
            provider=_Provider(4_000),
            system_messages=system,
            tools=tools,
            reserved_output_tokens=500,
            safety_margin_tokens=100,
        )
        large = calculate_dynamic_prompt_budget(
            provider=_Provider(8_000),
            system_messages=system,
            tools=tools,
            reserved_output_tokens=500,
            safety_margin_tokens=100,
        )
        self.assertGreater(large.usable_input_tokens, small.usable_input_tokens)
        self.assertLess(small.usable_input_tokens, 4_000 - 500 - 100)
        self.assertLess(small.compaction_target, small.soft_compaction_trigger)
        self.assertLess(small.soft_compaction_trigger, small.hard_input_limit)

    def test_hard_guard_raises_before_an_over_limit_call(self) -> None:
        provider = _Provider(1_000)
        budget = calculate_dynamic_prompt_budget(
            provider=provider,
            reserved_output_tokens=100,
            safety_margin_tokens=50,
        )
        messages = [{"role": "user", "content": "x" * 10_000}]
        with self.assertRaises(PromptBudgetExceeded):
            enforce_hard_token_guard(messages, budget=budget, provider=provider)

    def test_recent_selection_never_splits_a_tool_transaction(self) -> None:
        groups = [
            [{"role": "user", "content": "old " * 500}],
            [
                {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
                {"role": "tool", "tool_call_id": "c1", "content": "result"},
            ],
            [{"role": "user", "content": "latest"}],
        ]
        selected = select_complete_groups(
            groups,
            max_tokens=100,
            required_group_indexes=[2],
        )
        roles = [message["role"] for message in selected]
        self.assertEqual(["assistant", "tool", "user"], roles)

    def test_routed_model_budget_reaches_coding_context_assembly(self) -> None:
        observed = []
        for window in (16_000, 32_000):
            session = Session(
                id=f"task:window-{window}",
                active_agent="coding",
            )
            session.add_message("user", "inspect the current context budget")
            ContextBuilder(
                context_providers=DEFAULT_CONTEXT_PROVIDERS,
                coding_context_view_builder=build_coding_context_view,
            ).build(
                session=session,
                profile=SimpleNamespace(system_prompt="base", tool_mode="coding"),
                active_turn_start_index=0,
                model_provider=_Provider(window),
                model_tools=[{
                    "type": "function",
                    "function": {"name": "read", "parameters": {}},
                }],
                reserved_output_tokens=500,
            )
            observed.append(
                session.metadata[CODING_CONTEXT_STATE_METADATA_KEY]["metrics"][
                    "usable_input_tokens"
                ]
            )

        self.assertGreater(observed[1], observed[0])


if __name__ == "__main__":
    unittest.main()
