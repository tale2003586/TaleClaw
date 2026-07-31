from __future__ import annotations

import asyncio
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.sessions import Session, SessionManager
from tests.postgres_utils import temporary_postgres_schema
from web import server
from web.server import AgentService
from web.session_titles import (
    SessionTitleResult,
    WebSessionTitleService,
    first_question_answer,
    normalize_session_title,
    session_display_title,
)


class RecordingRunner:
    def __init__(self, result: str = "会话标题", error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def _session(*messages, title: str = "") -> Session:
    metadata = {"title": title} if title else {}
    return Session(id="web:local:demo", messages=list(messages), metadata=metadata)


def test_first_question_answer_uses_earliest_complete_pair_only():
    messages = [
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "first question"},
        {"role": "tool", "content": "tool output"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "later secret"},
        {"role": "assistant", "content": "later answer"},
    ]

    assert first_question_answer(messages) == ("first question", "first answer")
    assert first_question_answer(messages[:3]) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  “跨 Session\n记忆重构主题”  ", "跨 Session 记忆重构主题"),
        ("'English   title'", "English title"),
        ("`emoji 😀 标题`", "emoji 😀 标题"),
        ("一二三四五六七八九十一二三四五六七八九十二一", "一二三四五六七八九十一二三四五六七八九十"),
    ],
)
def test_normalize_session_title_cleans_and_limits_unicode(raw, expected):
    title = normalize_session_title(raw)

    assert title == expected
    assert len(title) <= 20


def test_session_display_title_prefers_title_without_changing_id():
    row = {
        "id": "web:local:stable-id",
        "metadata": {"title": "显示主题"},
    }

    assert session_display_title(row, "stable-id") == "显示主题"
    assert session_display_title({"metadata": {}}, "stable-id") == "stable-id"
    assert row["id"] == "web:local:stable-id"


def test_service_generates_title_from_isolated_first_pair():
    runner = RecordingRunner("“长期记忆边界分析”")
    session = _session(
        {"role": "user", "content": "为什么新 Session 有历史？"},
        {"role": "assistant", "content": "原因是历史检索缺少 Session 边界。"},
        {"role": "user", "content": "later secret"},
        {"role": "assistant", "content": "later answer"},
    )

    result = asyncio.run(WebSessionTitleService(runner=runner).ensure_title(session))

    assert result.title == "长期记忆边界分析"
    assert result.source == "model"
    assert result.updated is True
    assert session.metadata["title"] == result.title
    prompt = runner.calls[0]["messages"]
    assert len(prompt) == 2
    assert "为什么新 Session 有历史？" in prompt[1]["content"]
    assert "原因是历史检索缺少 Session 边界。" in prompt[1]["content"]
    assert "later secret" not in prompt[1]["content"]
    assert runner.calls[0]["spec"].model_purpose == "summary"
    assert runner.calls[0]["max_tokens"] == 48


@pytest.mark.parametrize("model_result", ["", "  \n  "])
def test_service_falls_back_when_model_returns_blank(model_result):
    runner = RecordingRunner(model_result)
    session = _session(
        {"role": "user", "content": "  首个问题需要精简到二十个字符以内吗？  "},
        {"role": "assistant", "content": "是。"},
    )

    result = asyncio.run(WebSessionTitleService(runner=runner).ensure_title(session))

    assert result.source == "fallback"
    assert result.title == "首个问题需要精简到二十个字符以内吗？"[:20]
    assert len(result.title) <= 20


def test_service_falls_back_on_model_error():
    runner = RecordingRunner(error=RuntimeError("model down"))
    session = _session(
        {"role": "user", "content": "分析流式输出"},
        {"role": "assistant", "content": "正在分析。"},
    )

    result = asyncio.run(WebSessionTitleService(runner=runner).ensure_title(session))

    assert result.source == "fallback"
    assert result.title == "分析流式输出"


def test_service_falls_back_on_timeout():
    class SlowRunner(RecordingRunner):
        def run(self, **kwargs):
            self.calls.append(kwargs)
            time.sleep(0.03)
            return "too late"

    runner = SlowRunner()
    session = _session(
        {"role": "user", "content": "超时仍保留正文"},
        {"role": "assistant", "content": "正文已经完成。"},
    )

    result = asyncio.run(
        WebSessionTitleService(
            runner=runner,
            timeout_seconds=0.001,
        ).ensure_title(session)
    )

    assert result.source == "fallback"
    assert result.title == "超时仍保留正文"


def test_service_is_idempotent_when_title_exists():
    runner = RecordingRunner("replacement")
    session = _session(
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
        title="既有标题",
    )

    result = asyncio.run(WebSessionTitleService(runner=runner).ensure_title(session))

    assert result.source == "existing"
    assert result.updated is False
    assert result.title == "既有标题"
    assert runner.calls == []


def test_service_does_not_generate_without_complete_pair():
    runner = RecordingRunner()
    session = _session({"role": "user", "content": "question only"})

    result = asyncio.run(WebSessionTitleService(runner=runner).ensure_title(session))

    assert result.source == "unavailable"
    assert result.updated is False
    assert "title" not in session.metadata
    assert runner.calls == []


def test_web_api_projects_persisted_title_without_changing_chat_id():
    with temporary_postgres_schema("web_session_title_api") as dsn:
        manager = SessionManager(dsn)
        session = manager.get_or_create("web:alice:stable-id")
        session.metadata.update({"user_id": "alice", "title": "持久化主题"})
        session.add_message("user", "question")
        session.add_message("assistant", "answer")
        manager.save(session)
        manager.close()

        with patch.dict("os.environ", {"SESSION_DATABASE_URL": dsn}):
            rows = server.read_sessions("alice")
            detail = server.read_session("stable-id", user_id="alice")
            missing = server.read_session("new-id", user_id="alice")

    assert rows[0]["title"] == "持久化主题"
    assert rows[0]["chat_id"] == "stable-id"
    assert rows[0]["id"] == "web:alice:stable-id"
    assert detail["title"] == "持久化主题"
    assert detail["chat_id"] == "stable-id"
    assert missing["title"] == "new-id"


def test_agent_service_streams_body_before_generating_and_saving_title():
    events = []

    class Manager:
        def __init__(self):
            self.sessions = {}
            self.saved = []

        def get_or_create(self, session_id):
            return self.sessions.setdefault(session_id, Session(id=session_id))

        def save(self, session):
            self.saved.append((session.id, session.metadata.get("title", "")))

    manager = Manager()
    service = AgentService()

    class TitleService:
        async def ensure_title(self, session):
            events.append("title")
            assert [message["role"] for message in session.messages] == ["user", "assistant"]
            session.metadata["title"] = "首轮主题"
            return SessionTitleResult("首轮主题", "model", True)

    class Runtime:
        services = SimpleNamespace(session_manager=manager)

        async def run_message(self, *, content, channel, chat_id, metadata, on_text):
            session = manager.get_or_create(f"{channel}:{chat_id}")
            session.add_message("user", content)
            on_text("流式")
            events.append("delta-1")
            on_text("正文")
            events.append("delta-2")
            session.add_message("assistant", "流式正文")
            manager.save(session)
            await service._handle_outbound(
                SimpleNamespace(chat_id=chat_id, content="流式正文")
            )

    service._runtime = Runtime()
    service._session_title_service = TitleService()
    service._session_locks = {}
    emitted = []

    async def ask():
        service._loop = asyncio.get_running_loop()
        return await service._ask_async(
            session_id="stable-id",
            content="first question",
            user_id="local",
            user_role="admin",
            on_text=emitted.append,
        )

    reply = asyncio.run(ask())

    assert reply == "流式正文"
    assert emitted == ["流式", "正文"]
    assert events == ["delta-1", "delta-2", "title"]
    assert manager.saved[-1] == ("web:local:stable-id", "首轮主题")


def test_agent_service_title_failure_does_not_fail_completed_reply():
    session = _session(
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    )
    manager = SimpleNamespace(
        get_or_create=lambda session_id: session,
        save=lambda value: None,
    )
    service = AgentService()

    class TitleService:
        async def ensure_title(self, value):
            raise RuntimeError("title failed")

    class Runtime:
        services = SimpleNamespace(session_manager=manager)

        async def run_message(self, *, chat_id, **kwargs):
            await service._handle_outbound(SimpleNamespace(chat_id=chat_id, content="answer"))

    service._runtime = Runtime()
    service._session_title_service = TitleService()
    service._session_locks = {}

    async def ask():
        service._loop = asyncio.get_running_loop()
        return await service._ask_async(
            session_id="stable-id",
            content="question",
            user_id="local",
            user_role="admin",
        )

    assert asyncio.run(ask()) == "answer"


def test_agent_service_timeout_keeps_stream_and_persists_fallback_title():
    events = []

    class Manager:
        def __init__(self):
            self.session = Session(id="web:local:timeout")
            self.saved_titles = []

        def get_or_create(self, session_id):
            assert session_id == self.session.id
            return self.session

        def save(self, session):
            self.saved_titles.append(session.metadata.get("title", ""))

    class SlowRunner(RecordingRunner):
        def run(self, **kwargs):
            self.calls.append(kwargs)
            time.sleep(0.03)
            return "late model title"

    manager = Manager()
    service = AgentService()

    class Runtime:
        services = SimpleNamespace(session_manager=manager)

        async def run_message(self, *, content, channel, chat_id, metadata, on_text):
            manager.session.add_message("user", content)
            on_text("正文")
            events.append("delta")
            manager.session.add_message("assistant", "正常完成")
            manager.save(manager.session)
            await service._handle_outbound(
                SimpleNamespace(chat_id=chat_id, content="正常完成")
            )

    service._runtime = Runtime()
    service._session_title_service = WebSessionTitleService(
        runner=SlowRunner(),
        timeout_seconds=0.001,
    )
    service._session_locks = {}

    async def ask():
        service._loop = asyncio.get_running_loop()
        return await service._ask_async(
            session_id="timeout",
            content="超时后使用首问题",
            user_id="local",
            user_role="admin",
            on_text=lambda text: events.append("on_text"),
        )

    reply = asyncio.run(ask())

    assert reply == "正常完成"
    assert events == ["on_text", "delta"]
    assert manager.saved_titles[-1] == "超时后使用首问题"


def test_frontend_uses_title_for_display_but_chat_id_for_identity():
    sessions = Path("web/frontend/src/hooks/useSessions.ts").read_text(encoding="utf-8")
    shell = Path("web/frontend/src/app/AppShell.tsx").read_text(encoding="utf-8")
    chat = Path("web/frontend/src/pages/ChatPage.tsx").read_text(encoding="utf-8")

    assert 'session.channel === "web" ? String(session.chat_id || "")' in sessions
    assert '`${session.title || ""} ${sessionKey(session)}' in sessions
    assert "setActiveId(String(session.chat_id || id))" in sessions
    assert "session.title || id" in shell
    assert "active?.title || sessions.activeId" in chat
    assert "dangerouslySetInnerHTML" not in sessions + shell + chat
