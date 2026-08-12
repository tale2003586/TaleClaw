import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from models.provider import OpenAICompatibleProvider
from web.server import (
    RequestHandler,
    _build_subagent_logs,
    _parent_trace_events,
    _render_subagent_logs,
    _stream_event_projection,
    render_chat_markdown,
)


def _chunk(*, content=None, reasoning_content=None, tool_calls=None):
    delta = SimpleNamespace(
        content=content,
        reasoning_content=reasoning_content,
        tool_calls=tool_calls or [],
    )
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


class StreamingProviderTests(unittest.TestCase):
    def test_stream_chat_emits_reasoning_content_separately(self) -> None:
        chunks = [
            _chunk(reasoning_content="先分析问题。"),
            _chunk(reasoning_content="再组织答案。"),
            _chunk(content="最终回答"),
        ]
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=lambda **_kwargs: chunks),
            ),
        )
        text = []
        thinking = []

        response = OpenAICompatibleProvider(client).stream_chat(
            model="test-model",
            messages=[{"role": "user", "content": "test"}],
            tools=[],
            max_tokens=100,
            on_text=text.append,
            on_thinking=thinking.append,
        )

        self.assertEqual(["最终回答"], text)
        self.assertEqual(["先分析问题。", "再组织答案。"], thinking)
        self.assertEqual("先分析问题。再组织答案。", response.raw_message["reasoning_content"])

    def test_stream_chat_emits_text_and_reassembles_tool_arguments(self) -> None:
        chunks = [
            _chunk(content="正在"),
            _chunk(content="处理"),
            _chunk(tool_calls=[
                _tool_delta(0, call_id="call_1", name="read_", arguments='{"pa'),
            ]),
            _chunk(tool_calls=[
                _tool_delta(0, name="file", arguments='th":"README.md"}'),
            ]),
        ]

        class Completions:
            def __init__(self) -> None:
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return chunks

        completions = Completions()
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=completions),
        )
        emitted = []

        response = OpenAICompatibleProvider(client).stream_chat(
            model="test-model",
            messages=[{"role": "user", "content": "read it"}],
            tools=[{"type": "function"}],
            tool_choice="auto",
            max_tokens=100,
            on_text=emitted.append,
        )

        self.assertTrue(completions.kwargs["stream"])
        self.assertEqual(["正在", "处理"], emitted)
        self.assertEqual("正在处理", response.content)
        self.assertEqual("call_1", response.tool_calls[0].id)
        self.assertEqual("read_file", response.tool_calls[0].name)
        self.assertEqual({"path": "README.md"}, response.tool_calls[0].arguments)
        self.assertEqual("read_file", response.raw_message["tool_calls"][0]["function"]["name"])

    def test_responses_stream_emits_typed_text_deltas_and_final_usage(self) -> None:
        tool_item = SimpleNamespace(
            type="function_call",
            id="item_1",
            call_id="call_1",
            name="read_file",
            arguments='{"path":"README.md"}',
        )
        final_response = SimpleNamespace(
            output_text="正在处理",
            output=[tool_item],
            usage=SimpleNamespace(input_tokens=12, output_tokens=4, total_tokens=16),
        )
        events = [
            SimpleNamespace(type="response.output_text.delta", delta="正在"),
            SimpleNamespace(type="response.output_text.delta", delta="处理"),
            SimpleNamespace(type="response.completed", response=final_response),
        ]

        class Responses:
            def __init__(self) -> None:
                self.kwargs = None

            def create(self, **kwargs):
                self.kwargs = kwargs
                return events

        responses = Responses()
        emitted = []
        response = OpenAICompatibleProvider(
            SimpleNamespace(responses=responses),
            wire_api="responses",
        ).stream_chat(
            model="test-model",
            messages=[{"role": "user", "content": "read it"}],
            tools=[],
            max_tokens=100,
            on_text=emitted.append,
        )

        self.assertTrue(responses.kwargs["stream"])
        self.assertEqual(["正在", "处理"], emitted)
        self.assertEqual("正在处理", response.content)
        self.assertEqual("call_1", response.tool_calls[0].id)
        self.assertEqual({"path": "README.md"}, response.tool_calls[0].arguments)
        self.assertEqual(12, response.usage.input_tokens)
        self.assertEqual(4, response.usage.output_tokens)
        self.assertEqual(16, response.usage.total_tokens)

    def test_responses_stream_reassembles_function_call_events(self) -> None:
        added_item = SimpleNamespace(
            type="function_call",
            id="item_1",
            call_id="call_1",
            name="read_file",
            arguments="",
        )
        events = [
            SimpleNamespace(
                type="response.output_item.added",
                output_index=0,
                item=added_item,
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=0,
                delta='{"path":"READ',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.delta",
                output_index=0,
                delta='ME.md"}',
            ),
            SimpleNamespace(
                type="response.function_call_arguments.done",
                output_index=0,
                name="read_file",
                arguments='{"path":"README.md"}',
            ),
        ]
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: events),
        )

        response = OpenAICompatibleProvider(
            client,
            wire_api="responses",
        ).stream_chat(
            model="test-model",
            messages=[],
            tools=[],
            max_tokens=100,
            on_text=lambda text: None,
        )

        self.assertIsNone(response.content)
        self.assertEqual("call_1", response.tool_calls[0].id)
        self.assertEqual("read_file", response.tool_calls[0].name)
        self.assertEqual({"path": "README.md"}, response.tool_calls[0].arguments)

    def test_responses_stream_raises_error_event(self) -> None:
        events = [SimpleNamespace(
            type="error",
            error=SimpleNamespace(message="relay stream failed"),
        )]
        client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: events),
        )

        with self.assertRaisesRegex(RuntimeError, "relay stream failed"):
            OpenAICompatibleProvider(client, wire_api="responses").stream_chat(
                model="test-model",
                messages=[],
                tools=[],
                max_tokens=100,
                on_text=lambda text: None,
            )


class StreamingHttpTests(unittest.TestCase):
    def test_chat_markdown_renders_formatting_and_escapes_unsafe_content(self) -> None:
        html = render_chat_markdown(
            "# 标题\n\n"
            "**重点** [安全链接](https://example.com) "
            "[危险链接](javascript:alert(1))\n\n"
            "<script>alert('x')</script>\n\n"
            "![远程图](https://example.com/image.png)\n"
        )

        self.assertIn("<h1>标题</h1>", html)
        self.assertIn("<strong>重点</strong>", html)
        self.assertIn('href="https://example.com"', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn("<script>", html)
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("[image: 远程图]", html)

    def test_chat_markdown_renders_tables(self) -> None:
        html = render_chat_markdown(
            "| 操作 | 写法 | 说明 |\n"
            "|------|------|------|\n"
            "| 创建 | `s = set()` | 空集合 |\n"
        )

        self.assertIn("<table>", html)
        self.assertIn("<th>操作</th>", html)
        self.assertIn("<td>创建</td>", html)
        self.assertIn("<code>s = set()</code>", html)

    def test_chat_stream_endpoint_returns_delta_and_complete_events(self) -> None:
        class AgentService:
            def subscribe_session(self, session_key, cb):
                self.session_key = session_key
                cb({
                    "event": "tool.call.started",
                    "run_id": "run_123",
                    "step": 1,
                    "span_id": "run_123:tool:1:call_1",
                    "payload": {
                        "tool_name": "read_file",
                        "arguments_preview": '{"path":"README.md"}',
                    },
                })
                self.unsubscribed = False

                def unsub():
                    self.unsubscribed = True

                return unsub

            def ask_stream(
                self,
                *,
                session_id,
                content,
                user_id,
                user_role,
                workspace_root=None,
                thinking_enabled=False,
                on_text,
            ):
                self.request = (session_id, content)
                self.user = (user_id, user_role)
                self.workspace_root = workspace_root
                self.thinking_enabled = thinking_enabled
                on_text("你")
                on_text("好")
                on_text.on_assistant_segment({
                    "step": 1,
                    "has_content": True,
                    "final": True,
                })
                on_text.on_assistant_completed({
                    "step": 1,
                    "reason": "assistant_final_message",
                })
                return "你好"

        agent_service = AgentService()
        handler = object.__new__(RequestHandler)
        handler.agent_service = agent_service
        handler._read_json_body = lambda: {
            "session_id": "default",
            "message": "hello",
            "workspace_root": "/tmp/project",
        }
        handler._send_stream_headers = lambda: None
        events = []
        handler._send_stream_event = events.append

        with patch(
            "web.server.read_session",
            return_value={"messages": [], "title": "首轮主题"},
        ):
            handler._handle_chat_stream()

        self.assertEqual(("default", "hello"), agent_service.request)
        self.assertEqual(("local", "admin"), agent_service.user)
        self.assertEqual("/tmp/project", agent_service.workspace_root)
        self.assertFalse(agent_service.thinking_enabled)
        self.assertEqual("web:local:default", agent_service.session_key)
        self.assertTrue(agent_service.unsubscribed)
        self.assertEqual(
            [
                "event",
                "delta",
                "delta",
                "assistant_segment",
                "assistant_completed",
                "complete",
            ],
            [event["type"] for event in events],
        )
        self.assertEqual("tool.call.started", events[0]["event"])
        self.assertEqual("read_file", events[0]["tool"])
        self.assertEqual('{"path":"README.md"}', events[0]["args"])
        self.assertEqual("你好", events[-1]["reply"])
        self.assertEqual("首轮主题", events[-1]["session"]["title"])

    def test_chat_attachment_is_parsed_before_combined_message_reaches_agent(self) -> None:
        from web.mineru import MinerUResult

        class AgentService:
            def ask_stream(self, **kwargs):
                self.request = kwargs
                kwargs["on_text"]("完成")
                return "完成"

        class FakeMinerU:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def parse_file(self, path):
                self.path = path
                return MinerUResult(path.name, "# 解析正文", "batch-1", "https://example.test/result.zip")

        agent_service = AgentService()
        handler = object.__new__(RequestHandler)
        handler.agent_service = agent_service
        handler._current_user = lambda: SimpleNamespace(user_id="alice", role="user")
        handler._read_json_body = lambda: {
            "session_id": "default",
            "message": "总结这份报告",
            "attachments": ["chat-attachments/report.pdf"],
        }
        handler._send_stream_headers = lambda: None
        events = []
        handler._send_stream_event = events.append

        with (
            patch("web.server._safe_storage_path", return_value=Path(__file__)),
            patch("web.mineru.MinerUClient", FakeMinerU),
            patch("web.server.read_session", return_value={"messages": [], "title": "报告"}),
        ):
            handler._handle_chat_stream()

        self.assertEqual("总结这份报告", agent_service.request["content"])
        self.assertNotIn("# 解析正文", agent_service.request["content"])
        self.assertEqual(
            "# 解析正文",
            agent_service.request["attachments"][0]["content"],
        )
        self.assertEqual("总结这份报告\n\n附件：test_web_streaming.py", agent_service.request["display_content"])
        self.assertEqual(["status", "status", "delta", "complete"], [event["type"] for event in events])

    def test_stream_event_projection_is_whitelisted_and_small(self) -> None:
        projected = _stream_event_projection({
            "event": "model.call.completed",
            "run_id": "run_123",
            "step": 2,
            "payload": {
                "model": "test-model",
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "total_tokens": 125,
                },
                "context_report": {"large": "x" * 1000},
                "api_key": "secret",
            },
        })

        self.assertEqual({
            "event",
            "run_id",
            "step",
            "model",
            "tokens",
            "input_tokens",
            "output_tokens",
        }, set(projected))
        self.assertEqual(125, projected["tokens"])
        self.assertNotIn("context_report", projected)
        self.assertNotIn("api_key", projected)

        self.assertIsNone(_stream_event_projection({
            "event": "context.build.completed",
            "payload": {"context_report": {"large": "x" * 1000}},
        }))

    def test_stream_headers_disable_nginx_buffering(self) -> None:
        handler = object.__new__(RequestHandler)
        headers = []
        handler.send_response = lambda status: None
        handler._send_cors_headers = lambda: None
        handler.send_header = lambda name, value: headers.append((name, value))
        handler.end_headers = lambda: None

        handler._send_stream_headers()

        self.assertIn(("X-Accel-Buffering", "no"), headers)
        self.assertIn(("Content-Type", "application/x-ndjson; charset=utf-8"), headers)


class WebRunTraceTests(unittest.TestCase):
    def test_subagent_events_are_grouped_for_run_detail(self) -> None:
        events = [
            {
                "timestamp": "2026-06-18T10:00:00Z",
                "session_id": "web:parent",
                "event": "tool.call.completed",
                "payload": {"tool_name": "parallel_tasks"},
            },
            {
                "timestamp": "2026-06-18T10:00:01Z",
                "session_id": "subtask:explore:abc123",
                "event": "subagent.started",
                "span_id": "run:tool:1:subagent:0",
                "parent_span_id": "run:tool:1",
                "payload": {
                    "agent_type": "explore",
                    "description": "memory scan",
                    "prompt_preview": "Inspect memory2",
                },
            },
            {
                "timestamp": "2026-06-18T10:00:02Z",
                "session_id": "subtask:explore:abc123",
                "event": "tool.call.completed",
                "step": 1,
                "span_id": "run:tool:1:subagent:0:tool:1:call-read",
                "parent_span_id": "run:tool:1:subagent:0:step:1",
                "payload": {
                    "tool_name": "read_file",
                    "status": "success",
                    "output_preview": "class Retriever",
                },
            },
            {
                "timestamp": "2026-06-18T10:00:03Z",
                "session_id": "subtask:explore:abc123",
                "event": "subagent.completed",
                "span_id": "run:tool:1:subagent:0",
                "parent_span_id": "run:tool:1",
                "payload": {
                    "agent_type": "explore",
                    "success": True,
                    "truncated": False,
                    "tool_count": 1,
                    "reasoning_steps": 2,
                    "summary_preview": "memory2 uses retriever.py",
                },
            },
        ]

        subagents = _build_subagent_logs(events)
        parent_events = _parent_trace_events(events)
        html = _render_subagent_logs(subagents)

        self.assertEqual(1, len(subagents))
        self.assertEqual(["web:parent"], [event["session_id"] for event in parent_events])
        self.assertEqual("subtask:explore:abc123", subagents[0]["session_id"])
        self.assertEqual("memory scan", subagents[0]["description"])
        self.assertEqual(["read_file"], subagents[0]["tools"])
        self.assertEqual(1, subagents[0]["tool_calls"])
        self.assertIn("memory scan", html)
        self.assertIn("class Retriever", html)
        self.assertIn("memory2 uses retriever.py", html)
        self.assertIn('<details class="subagent">', html)
        self.assertNotIn('<details class="subagent" open>', html)


if __name__ == "__main__":
    unittest.main()
