import unittest
from types import SimpleNamespace
from unittest.mock import patch

from models.provider import OpenAICompatibleProvider
from web.server import (
    RequestHandler,
    _build_subagent_logs,
    _parent_trace_events,
    _render_subagent_logs,
    render_chat_markdown,
)


def _chunk(*, content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls or [])
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _tool_delta(index, *, call_id=None, name=None, arguments=None):
    function = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=call_id, function=function)


class StreamingProviderTests(unittest.TestCase):
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
            def ask_stream(
                self,
                *,
                session_id,
                content,
                user_id,
                user_role,
                workspace_root=None,
                on_text,
            ):
                self.request = (session_id, content)
                self.user = (user_id, user_role)
                self.workspace_root = workspace_root
                on_text("你")
                on_text("好")
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

        with patch("web.server.read_session", return_value={"messages": []}):
            handler._handle_chat_stream()

        self.assertEqual(("default", "hello"), agent_service.request)
        self.assertEqual(("local", "admin"), agent_service.user)
        self.assertEqual("/tmp/project", agent_service.workspace_root)
        self.assertEqual(["delta", "delta", "complete"], [event["type"] for event in events])
        self.assertEqual("你好", events[-1]["reply"])

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
