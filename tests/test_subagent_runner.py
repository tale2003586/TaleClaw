import unittest
import json
import tempfile
from pathlib import Path

from agents.subagent.runner import TaskSubagentRunner
from models.provider import LLMResponse, ToolCall
from runtime.context import ContextBundle
from runtime.failure_reasons import SubagentFailureReason
from runtime.pipeline import Pipeline
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from sessions import Session
from tools.executor import ToolExecutor
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry


class TinyContextBuilder:
    def build(self, *, session, profile, **kwargs):
        return ContextBundle(messages=[
            {"role": "system", "content": profile.system_prompt},
            *session.messages,
        ])


class FinalAnswerProvider:
    def __init__(self, answer="subagent summary") -> None:
        self.answer = answer
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return LLMResponse(
            content=self.answer,
            raw_message={"role": "assistant", "content": self.answer},
        )


class RepeatingToolProvider:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        index = len(self.calls)
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id=f"call-{index}",
                    name="read_file",
                    arguments={"path": f"file_{index}.py"},
                )
            ],
            raw_message={
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call-{index}",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": "{}",
                    },
                }],
            },
        )


class ToolThenFinalProvider:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-read",
                        name="read_file",
                        arguments={"path": "README.md"},
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-read",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": "{\"path\":\"README.md\"}",
                        },
                    }],
                },
            )
        return LLMResponse(
            content="subagent traced summary",
            raw_message={
                "role": "assistant",
                "content": "subagent traced summary",
            },
        )


class MissingFileThenFinalProvider:
    def __init__(self) -> None:
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if len(self.calls) == 1:
            return LLMResponse(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="call-missing",
                        name="read_file",
                        arguments={"path": "/outside/workspace.py"},
                    )
                ],
                raw_message={
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call-missing",
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": "{\"path\":\"/outside/workspace.py\"}",
                        },
                    }],
                },
            )
        return LLMResponse(
            content="I could not inspect the target file.",
            raw_message={
                "role": "assistant",
                "content": "I could not inspect the target file.",
            },
        )


def _registry(read_file_handler=None) -> ToolRegistry:
    registry = ToolRegistry()
    for name in [
        "bash",
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "git_status",
        "git_diff",
        "git_log",
        "git_branch",
        "git_add",
        "git_commit",
        "background_run",
        "check_background",
        "memorize",
        "recall_memory",
        "storage_list_files",
        "storage_read_file",
        "task",
        "parallel_tasks",
        "spawn_teammate",
        "tool_search",
    ]:
        handler = read_file_handler if name == "read_file" and read_file_handler else (lambda **kwargs: "ok")
        registry.register(
            function_tool(name, f"{name} tool", {}, []),
            handler,
            enabled_modes={"coding"},
        )
    return registry


def _pipeline(provider=None, registry=None) -> Pipeline:
    return Pipeline(
        tools=registry or _registry(),
        provider=provider or FinalAnswerProvider(),
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=TinyContextBuilder(),
        max_reasoning_steps=3,
    )


class SubagentRunnerTests(unittest.TestCase):
    def test_unknown_agent_type_returns_structured_error(self) -> None:
        runner = TaskSubagentRunner(base_pipeline=_pipeline())

        result = runner.run(prompt="inspect", agent_type="researcher")

        self.assertFalse(result.success)
        self.assertEqual("researcher", result.agent_type)
        self.assertIn("Unknown agent_type", result.error)
        self.assertEqual(SubagentFailureReason.UNKNOWN_AGENT_TYPE.value, result.failure_reason)
        self.assertEqual("failed", result.status)
        self.assertTrue(result.recoverable)

    def test_tool_whitelists_prevent_recursion_and_limit_write_tools(self) -> None:
        runner = TaskSubagentRunner(base_pipeline=_pipeline())

        explore = runner._filtered_tools("explore")._tools
        plan = runner._filtered_tools("plan")._tools
        code = runner._filtered_tools("code")._tools

        self.assertIn("read_file", explore)
        self.assertNotIn("write_file", explore)
        self.assertNotIn("edit_file", explore)
        self.assertNotIn("write_file", plan)
        self.assertNotIn("edit_file", plan)
        self.assertIn("write_file", code)
        self.assertIn("edit_file", code)
        for tools in (explore, plan, code):
            self.assertNotIn("task", tools)
            self.assertNotIn("parallel_tasks", tools)
            self.assertNotIn("spawn_teammate", tools)

    def test_run_uses_isolated_session_and_returns_summary(self) -> None:
        provider = FinalAnswerProvider("done from subagent")
        runner = TaskSubagentRunner(base_pipeline=_pipeline(provider))
        parent = Session(
            id="parent",
            metadata={
                "workspace_root": "/tmp/work",
                "workspace_display_name": "work",
                "user_id": "u1",
            },
        )

        result = runner.run(
            prompt="List files",
            description="explore files",
            agent_type="explore",
            parent_session=parent,
        )

        self.assertTrue(result.success)
        self.assertEqual("done from subagent", result.summary)
        self.assertEqual(1, len(provider.calls))
        messages = provider.calls[0]["messages"]
        self.assertTrue(any("List files" in message["content"] for message in messages))
        self.assertTrue(any(
            tool["function"]["name"] == "read_file"
            for tool in provider.calls[0]["tools"]
        ))

    def test_run_extracts_structured_findings_from_json_summary(self) -> None:
        answer = json.dumps({
            "findings": [
                {
                    "path": "agent/looping/core.py",
                    "lines": "10-24",
                    "note": "Loop entry point.",
                }
            ],
            "incomplete": False,
        })
        provider = FinalAnswerProvider(answer)
        runner = TaskSubagentRunner(base_pipeline=_pipeline(provider))

        result = runner.run(
            prompt="extract facts",
            description="structured explore",
            agent_type="explore",
        )

        self.assertTrue(result.success)
        self.assertFalse(result.incomplete)
        self.assertEqual("agent/looping/core.py", result.findings[0]["path"])

    def test_run_extracts_structured_failure_protocol(self) -> None:
        answer = json.dumps({
            "findings": [
                {
                    "path": "agents/subagent/runner.py",
                    "lines": "80-130",
                    "note": "Runner needs narrower scope.",
                }
            ],
            "incomplete": True,
            "failure_reason": "subagent_scope_too_broad",
            "failure_message": "Scope was too broad for the subagent budget.",
            "recoverable": True,
            "retry_hint": "Retry with one file at a time.",
            "evidence": [{"files_seen": 1}],
        })
        provider = FinalAnswerProvider(answer)
        runner = TaskSubagentRunner(base_pipeline=_pipeline(provider))

        result = runner.run(
            prompt="extract facts",
            description="structured failure",
            agent_type="explore",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.incomplete)
        self.assertEqual("partial", result.status)
        self.assertEqual(SubagentFailureReason.SCOPE_TOO_BROAD.value, result.failure_reason)
        self.assertEqual("Retry with one file at a time.", result.retry_hint)
        self.assertEqual([{"files_seen": 1}], result.evidence)

    def test_step_limit_marks_subagent_result_incomplete(self) -> None:
        provider = RepeatingToolProvider()
        runner = TaskSubagentRunner(
            base_pipeline=_pipeline(provider),
            max_reasoning_steps=2,
        )

        result = runner.run(
            prompt="keep inspecting",
            description="looping explore",
            agent_type="explore",
        )

        self.assertFalse(result.success)
        self.assertTrue(result.truncated)
        self.assertEqual("reasoning_step_limit", result.stop_reason)
        self.assertIn("[INCOMPLETE: hit step limit]", result.summary)
        self.assertEqual(2, len(provider.calls))
        self.assertEqual("failed", result.status)
        self.assertEqual(SubagentFailureReason.STEP_LIMIT.value, result.failure_reason)
        self.assertTrue(result.recoverable)

    def test_tool_error_classifies_missing_required_files(self) -> None:
        provider = MissingFileThenFinalProvider()
        registry = _registry(
            read_file_handler=lambda **kwargs: (
                "Error: ValueError: Path escapes workspace: /outside/workspace.py"
            )
        )
        runner = TaskSubagentRunner(base_pipeline=_pipeline(provider, registry=registry))

        result = runner.run(
            prompt="read target",
            description="missing file",
            agent_type="explore",
        )

        self.assertFalse(result.success)
        self.assertEqual("failed", result.status)
        self.assertEqual(
            SubagentFailureReason.MISSING_REQUIRED_FILES.value,
            result.failure_reason,
        )
        self.assertIn("verified paths", result.retry_hint)
        self.assertEqual(1, result.tool_count)

    def test_subagent_writes_execution_events_into_parent_trace(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            trace_store = TraceStore(Path(tmp) / ".runs")
            parent_run = RunState.create(
                session_id="web:parent",
                run_id="run_parent_trace",
            )
            trace_store.start_run(parent_run)
            provider = ToolThenFinalProvider()
            runner = TaskSubagentRunner(
                base_pipeline=_pipeline(provider),
                max_reasoning_steps=4,
            )
            parent = Session(id="web:parent", metadata={"user_role": "admin"})

            result = runner.run(
                prompt="Inspect README",
                description="trace explore",
                agent_type="explore",
                parent_session=parent,
                trace_store=trace_store,
                parent_run_state=parent_run,
                parent_span_id="run_parent_trace:step:1:tool:parallel",
            )

            self.assertTrue(result.success)
            run_dir = trace_store.run_dir(parent_run)
            events = [
                json.loads(line)
                for line in (run_dir / "trace.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            subagent_session_ids = {
                event["session_id"]
                for event in events
                if str(event.get("session_id") or "").startswith("subtask:explore:")
            }
            self.assertEqual(1, len(subagent_session_ids))
            subagent_session_id = next(iter(subagent_session_ids))
            event_names = [
                event["event"]
                for event in events
                if event.get("session_id") == subagent_session_id
            ]
            self.assertIn("subagent.started", event_names)
            self.assertIn("subagent.completed", event_names)
            self.assertIn("model.call.completed", event_names)
            self.assertIn("tool.call.completed", event_names)
            tool_event = next(
                event
                for event in events
                if event.get("session_id") == subagent_session_id
                and event.get("event") == "tool.call.completed"
            )
            self.assertEqual("read_file", tool_event["payload"]["tool_name"])
            completed_event = next(
                event
                for event in events
                if event.get("session_id") == subagent_session_id
                and event.get("event") == "subagent.completed"
            )
            self.assertEqual("completed", completed_event["payload"]["status"])
            self.assertIsNone(completed_event["payload"]["failure_reason"])
            step_event = next(
                event
                for event in events
                if event.get("session_id") == subagent_session_id
                and event.get("event") == "reasoning.step.started"
            )
            self.assertEqual(
                "run_parent_trace:step:1:tool:parallel:subagent:0",
                step_event["parent_span_id"],
            )
            self.assertTrue(
                tool_event["parent_span_id"].startswith(
                    "run_parent_trace:step:1:tool:parallel:subagent:0:step:"
                )
            )
            state = json.loads((run_dir / "run_state.json").read_text(encoding="utf-8"))
            self.assertEqual("web:parent", state["session_id"])


if __name__ == "__main__":
    unittest.main()
