from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from runtime.sessions import Session, SessionManager
from tests.postgres_utils import temporary_postgres_schema
from web import server
from web.session_titles import (
    first_question_answer,
    normalize_session_title,
    session_display_title,
)


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


def test_process_worker_saves_first_question_fallback_before_exit():
    from web.run_worker import _save_fallback_title
    session = _session(
        {"role": "user", "content": "  “如何配置\n并行任务”  "},
        {"role": "assistant", "content": "配置完成"},
    )
    saved = []
    manager = SimpleNamespace(get_or_create=lambda _: session, save=lambda value: saved.append(value.metadata["title"]))
    runtime = SimpleNamespace(services=SimpleNamespace(session_manager=manager))
    _save_fallback_title(runtime, session.id)
    assert saved == ["如何配置 并行任务"]
    session.messages.extend([
        {"role": "user", "content": "A later task must not rename this session"},
        {"role": "assistant", "content": "Later answer"},
    ])
    _save_fallback_title(runtime, session.id)
    assert saved == ["如何配置 并行任务"]
    assert session.metadata["title"] == "如何配置 并行任务"


def test_process_worker_preserves_existing_title_without_post_run_writer():
    from web.run_worker import _save_fallback_title
    session = _session(
        {"role": "user", "content": "another question"},
        {"role": "assistant", "content": "answer"},
    )
    session.metadata["title"] = "用户命名"
    saved = []
    manager = SimpleNamespace(get_or_create=lambda _: session, save=saved.append)
    runtime = SimpleNamespace(services=SimpleNamespace(session_manager=manager))
    _save_fallback_title(runtime, session.id)
    assert session.metadata["title"] == "用户命名"
    assert saved == []


def test_process_worker_skips_title_without_complete_message_pair():
    from web.run_worker import _save_fallback_title
    session = _session({"role": "user", "content": "incomplete"})
    saved = []
    manager = SimpleNamespace(get_or_create=lambda _: session, save=saved.append)
    _save_fallback_title(SimpleNamespace(services=SimpleNamespace(session_manager=manager)), session.id)
    assert saved == []


# Session title display versus chat identity is exercised through rendered UI
# and requests in frontend/src/test/chat-sessions.test.tsx and
# frontend/src/test/session-cache.test.tsx, rather than source-string matching.
