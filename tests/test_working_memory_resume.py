import unittest
from types import SimpleNamespace

from models.provider import LLMResponse
from runtime.failure_reasons import (
    REASONING_LOOP_STOP_REASON_KEY,
    StopReason,
)
from runtime.pipeline import Pipeline
from runtime.working_memory import (
    STATUS_COMPLETED,
    STATUS_RUNNING,
    STATUS_SUSPENDED,
    WORKING_MEMORY_METADATA_KEY,
    checkpoint_subtask_results,
    checkpoint_subtasks_dispatched,
    load_working_memory,
    prepare_working_memory_for_turn,
    render_working_memory_block,
)
from sessions.session import Session
from tools.executor import ToolExecutor
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


def _pipeline(provider: CountingProvider) -> Pipeline:
    return Pipeline(
        tools=ToolRegistry(),
        provider=provider,
        model="test-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(),
        max_reasoning_steps=4,
    )


class WorkingMemoryResumeTests(unittest.TestCase):
    def test_subtask_checkpoint_moves_success_to_completed(self) -> None:
        session = Session(id="task:test", current_mode="coding")
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
                    "findings": [{"claim": "pipeline owns turn lifecycle"}],
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

    def test_cancel_requested_stops_before_model_call_and_suspends_memory(self) -> None:
        provider = CountingProvider()
        pipeline = _pipeline(provider)
        session = Session(id="task:cancel", current_mode="coding")
        session.add_message("user", "请检查四条线索")

        reply = pipeline.run(
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
        memory = session.metadata[WORKING_MEMORY_METADATA_KEY]
        self.assertEqual(STATUS_SUSPENDED, memory["status"])

    def test_successful_final_answer_marks_memory_completed(self) -> None:
        provider = CountingProvider()
        pipeline = _pipeline(provider)
        session = Session(id="task:done", current_mode="coding")
        session.add_message("user", "继续")
        prepare_working_memory_for_turn(
            session,
            objective="原始任务",
            resume_requested=True,
            task_id="task:done",
        )

        reply = pipeline.run(session, SimpleNamespace(tool_mode="coding"))

        self.assertEqual("done", reply)
        memory = load_working_memory(session)
        self.assertEqual(STATUS_COMPLETED, memory.status)


if __name__ == "__main__":
    unittest.main()
