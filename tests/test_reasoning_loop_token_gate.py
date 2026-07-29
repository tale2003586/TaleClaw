import unittest
from unittest.mock import patch
from types import SimpleNamespace

from models.provider import LLMResponse
from runtime.context.dynamic_budget import PromptBudgetExceeded
from runtime.execution.reasoning_loop import ReasoningLoop
from runtime.sessions.session import Session


class TinyContextProvider:
    context_limit = 120

    def __init__(self) -> None:
        self.calls = []

    def count_tokens(self, messages):
        return sum(len(str(message.get("content") or "")) for message in messages)

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content="done",
            raw_message={"role": "assistant", "content": "done"},
        )


class EmptyTools:
    def schemas_for_turn(self, session, mode):
        return []


class TraceStore:
    def __init__(self) -> None:
        self.events = []

    def append_event(self, run_state, event_name, payload, **kwargs):
        self.events.append((event_name, payload, kwargs))

    def write_run_state(self, run_state):
        return None


class RunState:
    run_id = "run-test"

    def record_reasoning_step(self, step):
        return None

    def record_tool(self, name):
        return None

    def stop(self, reason, message):
        return None


class ReasoningLoopTokenGateTests(unittest.TestCase):
    def test_model_call_trims_context_before_api(self) -> None:
        provider = TinyContextProvider()
        trace = TraceStore()
        session = Session(id="web:test")
        session.add_message("user", "x" * 1000)
        session.add_message("assistant", "old answer")
        session.add_message("user", "answer the latest question")
        loop = ReasoningLoop(
            tools=EmptyTools(),
            tool_executor=object(),
            max_reasoning_steps=2,
        )

        loop.run(
            session=session,
            profile=SimpleNamespace(tool_mode="bot"),
            build_context=lambda session, profile: SimpleNamespace(messages=list(session.messages), report=None),
            resolve_provider=lambda session, profile: (provider, "tiny-model"),
            after_turn=lambda session: None,
            run_state=RunState(),
            trace_store=trace,
        )

        self.assertTrue(any(event[0] == "context_emergency_trim" for event in trace.events))
        sent_messages = provider.calls[0]["messages"]
        self.assertLessEqual(provider.count_tokens(sent_messages), int(provider.context_limit * 0.85))
        self.assertEqual("answer the latest question", sent_messages[-1]["content"])

    def test_required_oversized_latest_request_blocks_before_provider_call(self) -> None:
        provider = TinyContextProvider()
        trace = TraceStore()
        session = Session(id="web:hard-budget")
        session.add_message("user", "required-current-request:" + ("z" * 1000))
        loop = ReasoningLoop(
            tools=EmptyTools(),
            tool_executor=object(),
            max_tokens=10,
            max_reasoning_steps=2,
        )

        with self.assertRaises(PromptBudgetExceeded):
            loop.run(
                session=session,
                profile=SimpleNamespace(tool_mode="bot"),
                build_context=lambda session, profile: SimpleNamespace(
                    messages=list(session.messages),
                    report=None,
                ),
                resolve_provider=lambda session, profile: (provider, "tiny-model"),
                after_turn=lambda session: None,
                run_state=RunState(),
                trace_store=trace,
            )

        self.assertEqual([], provider.calls)
        self.assertEqual(
            1,
            session.metadata["context_metrics"]["hard_budget_blocks"],
        )
        self.assertTrue(any(event[0] == "hard_budget_blocked" for event in trace.events))

    def test_hard_guard_cannot_be_disabled_with_dynamic_assembly_flag(self) -> None:
        provider = TinyContextProvider()
        session = Session(id="web:hard-budget-rollback")
        session.add_message("user", "required-current-request:" + ("z" * 1000))
        loop = ReasoningLoop(
            tools=EmptyTools(),
            tool_executor=object(),
            max_tokens=10,
            max_reasoning_steps=2,
        )

        with patch(
            "runtime.execution.reasoning_loop.DYNAMIC_PROMPT_BUDGET_ENABLED",
            False,
        ), self.assertRaises(PromptBudgetExceeded):
            loop.run(
                session=session,
                profile=SimpleNamespace(tool_mode="bot"),
                build_context=lambda session, profile: SimpleNamespace(
                    messages=list(session.messages),
                    report=None,
                ),
                resolve_provider=lambda session, profile: (provider, "tiny-model"),
                after_turn=lambda session: None,
                run_state=RunState(),
                trace_store=TraceStore(),
            )

        self.assertEqual([], provider.calls)
        self.assertEqual(1, session.metadata["context_metrics"]["hard_budget_blocks"])


if __name__ == "__main__":
    unittest.main()
