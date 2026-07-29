from __future__ import annotations

import json
from types import SimpleNamespace

from models.provider import LLMResponse, ToolCall
from runtime.context.events import ContextEventType, thaw
from runtime.execution.reasoning_loop import ReasoningLoop
from runtime.sessions import Session
from tools.executor import ToolExecutionResult


ARTIFACT_REF = {
    "artifact_id": "art_tool_result",
    "artifact_type": "tool_result",
    "name": "tool-output",
    "mime_type": "text/plain",
    "size_bytes": 4096,
    "size_chars": 4096,
    "content_hash": "a" * 64,
    "storage_uri": "artifact://art_tool_result",
}


class ArtifactResultExecutor:
    def __init__(self, output: str) -> None:
        self.output = output
        self.requests = []

    def execute(self, request, invoker) -> ToolExecutionResult:
        self.requests.append(request)
        return ToolExecutionResult(
            status="success",
            output=self.output,
            final_arguments=dict(request.arguments),
            duration_ms=1.0,
            metadata={
                "artifact_ref": dict(ARTIFACT_REF),
                "artifact_offloaded_chars": 4096,
                "artifact_offloaded_tokens": 1024,
            },
        )


class UnusedTools:
    def execution_error_for_turn(self, name, **kwargs):
        return None

    def execute(self, *args, **kwargs):
        raise AssertionError("the artifact executor should not invoke the tool registry")


def _response(calls: list[ToolCall]) -> LLMResponse:
    return LLMResponse(
        content=None,
        tool_calls=calls,
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {
                        "name": call.name,
                        "arguments": json.dumps(call.arguments),
                    },
                }
                for call in calls
            ],
        },
    )


def _loop(output: str) -> ReasoningLoop:
    return ReasoningLoop(
        tools=UnusedTools(),
        tool_executor=ArtifactResultExecutor(output),
        max_tokens=256,
    )


def _assert_artifact_precedes_results(
    session: Session,
    *,
    result_count: int,
    aggregate_call_id: str,
    aggregate_tool_name: str,
    related_call_ids: list[str],
) -> None:
    events = session.events_after(0)
    tool_calls = [
        event for event in events if event.type == ContextEventType.TOOL_CALL.value
    ]
    artifacts = [
        event for event in events if event.type == ContextEventType.ARTIFACT_CREATED.value
    ]
    tool_results = [
        event for event in events if event.type == ContextEventType.TOOL_RESULT.value
    ]

    assert len(tool_calls) == 1
    assert len(artifacts) == 1
    assert len(tool_results) == result_count
    assert tool_calls[0].seq < artifacts[0].seq < min(
        event.seq for event in tool_results
    )

    payload = thaw(artifacts[0].payload)
    assert payload["artifact_ref"] == ARTIFACT_REF
    assert payload["source"] == "tool_result"
    assert payload["tool_call_id"] == aggregate_call_id
    assert payload["tool_name"] == aggregate_tool_name
    assert payload["related_tool_call_ids"] == related_call_ids
    assert payload["artifact_offloaded_chars"] == 4096
    assert payload["artifact_offloaded_tokens"] == 1024

    for event in tool_results:
        result_message = thaw(event.payload)["message"]
        assert result_message["metadata"]["artifact_ref"] == ARTIFACT_REF


def test_normal_tool_result_records_artifact_before_result_backfill() -> None:
    call = ToolCall(id="call-normal", name="inspect", arguments={"path": "a.py"})
    response = _response([call])
    session = Session(id="artifact:normal")
    loop = _loop("artifact pointer")
    loop._after_reasoning_step(session, response)

    loop._execute_tool_calls(
        session,
        response,
        SimpleNamespace(tool_mode="coding"),
    )

    _assert_artifact_precedes_results(
        session,
        result_count=1,
        aggregate_call_id="call-normal",
        aggregate_tool_name="inspect",
        related_call_ids=["call-normal"],
    )


def test_parallel_task_result_records_one_artifact_before_split_results() -> None:
    calls = [
        ToolCall(id="call-a", name="task", arguments={"prompt": "alpha"}),
        ToolCall(id="call-b", name="task", arguments={"prompt": "beta"}),
    ]
    response = _response(calls)
    session = Session(id="artifact:parallel")
    loop = _loop(json.dumps({"results": [{"summary": "a"}, {"summary": "b"}]}))
    loop._after_reasoning_step(session, response)

    loop._execute_auto_parallelized_task_calls(
        session,
        calls,
        SimpleNamespace(tool_mode="coding"),
    )

    _assert_artifact_precedes_results(
        session,
        result_count=2,
        aggregate_call_id="auto_parallel:call-a",
        aggregate_tool_name="parallel_tasks",
        related_call_ids=["call-a", "call-b"],
    )


def test_batched_read_result_records_one_artifact_before_split_results() -> None:
    calls = [
        ToolCall(id="call-a", name="read_file", arguments={"path": "a.py"}),
        ToolCall(id="call-b", name="read_file", arguments={"path": "b.py"}),
    ]
    response = _response(calls)
    session = Session(id="artifact:batch-read")
    loop = _loop(json.dumps({
        "results": [
            {"path": "a.py", "output": "a"},
            {"path": "b.py", "output": "b"},
        ],
    }))
    loop._after_reasoning_step(session, response)

    loop._execute_auto_batched_read_file_calls(
        session,
        calls,
        SimpleNamespace(tool_mode="coding"),
    )

    _assert_artifact_precedes_results(
        session,
        result_count=2,
        aggregate_call_id="auto_read_files:call-a",
        aggregate_tool_name="read_files",
        related_call_ids=["call-a", "call-b"],
    )
