from unittest.mock import MagicMock, patch

from web import server


def test_read_sessions_page_limits_results_and_returns_cursor():
    store = MagicMock()
    store.list_sessions.return_value = [
        _session_row("web:alice:newest", "2026-07-29T12:00:00Z"),
        _session_row("web:alice:older", "2026-07-28T12:00:00Z"),
        _session_row("web:alice:oldest", "2026-07-27T12:00:00Z"),
    ]

    with patch("runtime.sessions.session_store.SessionStore", return_value=store):
        result = server.read_sessions_page("alice", limit=2, offset=0)

    assert [item["chat_id"] for item in result["sessions"]] == ["newest", "older"]
    assert result["has_more"] is True
    assert result["next_offset"] == 2
    store.list_sessions.assert_called_once_with(
        limit=3,
        offset=0,
        id_prefix="web:alice:",
    )
    store.close.assert_called_once()


def test_read_session_uses_message_cursor_without_loading_full_context():
    store = MagicMock()
    store.load_session_page.return_value = {
        **_session_row("web:alice:default", "2026-07-29T12:00:00Z"),
        "messages": [{"seq": 4, "role": "user", "content": "older"}],
        "message_page": {"has_more": True, "next_before": 4},
    }

    with patch("runtime.sessions.session_store.SessionStore", return_value=store):
        result = server.read_session(
            "default",
            user_id="alice",
            message_limit=40,
            before_seq=5,
        )

    assert result["chat_id"] == "default"
    assert result["messages"][0]["seq"] == 4
    assert result["message_page"] == {"has_more": True, "next_before": 4}
    store.load_session_page.assert_called_once_with(
        "web:alice:default",
        limit=40,
        before_seq=5,
    )
    store.load_session.assert_not_called()
    store.close.assert_called_once()


def _session_row(session_id: str, updated_at: str):
    return {
        "id": session_id,
        "active_agent": "hybrid",
        "created_at": updated_at,
        "updated_at": updated_at,
        "last_compacted": None,
        "metadata": {},
    }
