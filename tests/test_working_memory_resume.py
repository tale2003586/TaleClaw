import unittest
from types import SimpleNamespace

from models.provider import LLMResponse, ToolCall
from runtime.execution.failure_reasons import (
    REASONING_LOOP_STOP_REASON_KEY,
    StopReason,
)
from runtime.execution.loop_policies import standard_execution_policies
from runtime.runtime import Runtime
from runtime.tooling.signature import tool_call_signature
from runtime.working_memory import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SUSPENDED,
    WORKING_MEMORY_METADATA_KEY,
    WorkingMemory,
    checkpoint_subtask_results,
    checkpoint_subtasks_dispatched,
    load_working_memory,
    prepare_working_memory_for_turn,
    render_working_memory_block,
    save_working_memory,
)
from runtime.sessions.session import Session
from tools.executor import ToolExecutionRequest, ToolExecutor
from tools.hooks import ToolLoopGuardHook
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry


class ContextBuilder:
    def build(self, **kwargs):
        return SimpleNamespace(messages=kwargs["session"].messages)


class CountingProvider:
    def __init__(self, response: LLMResponse | None = None) -> None:
        self.calls = 0
        self.response = response or LLMResponse(
            content="done",
            raw_message={"role": "assistant", "content": "done"},
        )

    def chat(self, **kwargs):
        self.calls += 1
        return self.response


class ScriptedProvider:
    def __init__(self, responses) -> None:
        self.responses = list(responses)
        self.calls = 0

    def chat(self, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _pipeline(provider: CountingProvider) -> Runtime:
    return Runtime(
        tools=ToolRegistry(),
        provider=provider,
        model="test-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
        max_reasoning_steps=4,
        execution_policy_factory=standard_execution_policies,
    )


def _tool_response(index: int, name: str, arguments: dict | None = None) -> LLMResponse:
    arguments = arguments or {}
    return LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id=f"call-{index}",
                name=name,
                arguments=arguments,
            )
        ],
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": "{}",
                    },
                }
            ],
        },
    )


def _final_response(content: str) -> LLMResponse:
    return LLMResponse(
        content=content,
        raw_message={
            "role": "assistant",
            "content": content,
        },
    )


class WorkingMemoryResumeTests(unittest.TestCase):
    def test_old_payload_defaults_new_state_fields(self) -> None:
        memory = WorkingMemory.from_payload({
            "task_id": "task-old",
            "objective": "old task",
            "pending_units": [{
                "unit_id": "unit-old",
                "description": "old pending",
            }],
        })

        self.assertIsNotNone(memory)
        self.assertEqual([], memory.observed_calls)
        self.assertEqual("P1", memory.pending_units[0]["priority"])
        self.assertEqual("todo", memory.pending_units[0]["state"])
        self.assertEqual([], memory.pending_units[0]["blocked_by"])

    def test_tool_signature_matches_loop_guard_fingerprint(self) -> None:
        request = ToolExecutionRequest(
            call_id="call-1",
            tool_name="read_file",
            arguments={"path": "runtime/working_memory.py", "offset": 0},
            session_id="session",
        )
        hook = ToolLoopGuardHook()

        self.assertEqual(
            tool_call_signature(request.tool_name, request.arguments),
            hook._fingerprint(request),
        )

    def test_subtask_checkpoint_moves_success_to_completed(self) -> None:
        session = Session(id="task:test", active_agent="coding")
        prepare_working_memory_for_turn(
            session,
            objective="检查两个模块",
            task_id="task:test",
        )
        tasks = [
            {
                "description": "检查 runtime",
                "agent_type": "explore",
                "scope": {"files": ["runtime/pipeline.py"]},
            }
        ]

        checkpoint_subtasks_dispatched(session, tasks)
        memory = load_working_memory(session)
        self.assertIsNotNone(memory)
        self.assertEqual(STATUS_RUNNING, memory.status)
        self.assertEqual(1, len(memory.pending_units))

        checkpoint_subtask_results(
            session,
            tasks,
            [
                {
                    "agent_type": "explore",
                    "success": True,
                    "summary": "runtime 主链路已经确认",
                    "status": "completed",
                    "files_touched": ["runtime/pipeline.py"],
                    "findings": [
                        {
                            "claim": "pipeline owns turn lifecycle",
                            "path": "runtime/pipeline.py",
                            "lines": "1-20",
                            "confidence": "high",
                        }
                    ],
                    "evidence": [
                        {
                            "path": "runtime/pipeline.py",
                            "lines": "1-20",
                            "quote_or_signal": "class Runtime",
                        }
                    ],
                    "covered_scope": ["runtime/pipeline.py"],
                    "open_questions": ["which caller owns cancellation?"],
                    "needs_parent_verification": True,
                }
            ],
        )

        memory = load_working_memory(session)
        self.assertEqual([], memory.pending_units)
        self.assertEqual(1, len(memory.completed_units))
        self.assertIn("runtime 主链路", memory.completed_units[0]["conclusion"])
        rendered = render_working_memory_block(session)
        self.assertIn("<working-memory", rendered)
        self.assertIn("不要重做已完成线索", rendered)
        self.assertIn("需要父级复核: yes", rendered)
        self.assertIn("which caller owns cancellation?", rendered)

    def test_cancel_requested_stops_before_model_call_and_suspends_memory(self) -> None:
        provider = CountingProvider()
        pipeline = _pipeline(provider)
        session = Session(id="task:cancel", active_agent="coding")
        session.add_message("user", "请检查四条线索")

        reply = pipeline.run_turn(
            session,
            SimpleNamespace(tool_mode="coding"),
            cancel_requested=lambda: True,
        )

        self.assertEqual(0, provider.calls)
        self.assertIn("用户请求停止", reply)
        self.assertEqual(
            StopReason.USER_CANCELLED.value,
            session.metadata[REASONING_LOOP_STOP_REASON_KEY],
        )
        self.assertNotIn(WORKING_MEMORY_METADATA_KEY, session.metadata)
        memory = load_working_memory(session)
        self.assertIsNone(memory)

    def test_successful_final_answer_marks_memory_completed(self) -> None:
        provider = CountingProvider()
        pipeline = _pipeline(provider)
        session = Session(id="task:done", active_agent="coding")
        session.add_message("user", "继续")
        prepare_working_memory_for_turn(
            session,
            objective="原始任务",
            resume_requested=True,
            task_id="task:done",
        )

        reply = pipeline.run_turn(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual("done", reply)
        memory = load_working_memory(session)
        self.assertEqual(STATUS_RUNNING, memory.status)

    def test_coding_reasoning_steps_create_checkpoints(self) -> None:
        registry = ToolRegistry()
        registry.register(
            function_tool("read_file", "Read test file", {}, []),
            lambda **kwargs: "file contents",
            allowed_agents={"coding"},
            always_on=True,
        )
        provider = ScriptedProvider([
            _tool_response(1, "read_file", {"path": "README.md"}),
            _final_response("done"),
        ])
        pipeline = Runtime(
            tools=registry,
            provider=provider,
            model="test-model",
            tool_executor=ToolExecutor([]),
            context_builder=ContextBuilder(),
            max_reasoning_steps=4,
            execution_policy_factory=standard_execution_policies,
        )
        session = Session(id="task:checkpoint", active_agent="coding")
        session.add_message("user", "inspect then answer")
        prepare_working_memory_for_turn(
            session,
            objective="inspect then answer",
            task_id="task:checkpoint",
        )

        checkpoint_steps = []
        reply = pipeline.run_turn(
            session,
            SimpleNamespace(tool_mode="coding"),
            checkpoint_callback=lambda session: checkpoint_steps.append(
                load_working_memory(session).last_checkpoint_step
            ),
        )

        self.assertEqual("done", reply)
        self.assertEqual([0], checkpoint_steps)
        memory = load_working_memory(session)
        self.assertEqual(STATUS_RUNNING, memory.status)
        self.assertEqual(0, memory.last_checkpoint_step)
        self.assertEqual([], memory.step_checkpoints)

    def test_reasoning_checkpoint_writes_observed_ledger_and_deduplicates(self) -> None:
        registry = ToolRegistry()
        registry.register(
            function_tool("read_file", "Read test file", {}, []),
            lambda **kwargs: "def parse_config():\n    return {}\n",
            allowed_agents={"coding"},
            always_on=True,
        )
        provider = ScriptedProvider([
            _tool_response(1, "read_file", {"path": "src/config.py", "offset": 0}),
            _tool_response(2, "read_file", {"path": "src/config.py", "offset": 0}),
            _final_response("done"),
        ])
        pipeline = Runtime(
            tools=registry,
            provider=provider,
            model="test-model",
            tool_executor=ToolExecutor([]),
            context_builder=ContextBuilder(),
            max_reasoning_steps=5,
            execution_policy_factory=standard_execution_policies,
        )
        session = Session(id="task:ledger", active_agent="coding")
        session.add_message("user", "inspect config")
        prepare_working_memory_for_turn(
            session,
            objective="inspect config",
            task_id="task:ledger",
        )

        pipeline.run_turn(session, SimpleNamespace(tool_mode="coding"))

        memory = load_working_memory(session)
        self.assertEqual([], memory.observed_calls)
        rendered = render_working_memory_block(session)
        self.assertIn("<working-memory", rendered)

    def test_render_working_memory_has_next_queue_observed_and_protocol(self) -> None:
        session = Session(id="task:render", active_agent="coding")
        prepare_working_memory_for_turn(
            session,
            objective="render state",
            task_id="task:render",
        )
        checkpoint_subtasks_dispatched(
            session,
            [
                {
                    "unit_id": "unit-blocked",
                    "description": "blocked task",
                    "priority": "P0",
                    "blocked_by": ["unit-first"],
                    "scope": {"files": ["blocked.py"]},
                },
                {
                    "unit_id": "unit-first",
                    "description": "first task",
                    "priority": "P0",
                    "scope": {"files": ["first.py"]},
                },
                {
                    "unit_id": "unit-second",
                    "description": "second independent task",
                    "priority": "P0",
                    "scope": {"files": ["second.py"]},
                },
            ],
        )
        memory = load_working_memory(session)
        memory.observed_calls.append({
            "signature": tool_call_signature("rg", {"pattern": "TODO", "path": "src"}),
            "tool": "rg",
            "arguments": {"pattern": "TODO", "path": "src"},
            "label": "rg(pattern='TODO', path='src')",
            "gist": "命中 3 行，涉及 tests/",
            "step": 3,
            "info_gain": True,
            "count": 1,
        })
        save_working_memory(session, memory)

        rendered = render_working_memory_block(session)

        self.assertIn("下一步动作队列", rendered)
        self.assertIn("→ NEXT [P0] unit-first", rendered)
        self.assertIn("↔ PARALLEL [P0] unit-second", rendered)
        self.assertIn("prefer parallel_tasks", rendered)
        self.assertIn("blocked_by=unit-first", rendered)
        self.assertIn("已观察", rendered)
        self.assertIn("rg(pattern='TODO', path='src'): 命中 3 行", rendered)
        self.assertIn("<working-memory-protocol", rendered)
        self.assertIn("优先调用 parallel_tasks", rendered)


if __name__ == "__main__":
    unittest.main()
