"""HTTP contracts: authenticated identity, replay and owned Git delivery."""
import io
import json
from http import HTTPStatus
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from web.auth_store import AuthenticatedUser
from web.server import RequestHandler
from web.agent_service import RunConflictError, RunNotFoundError, RunCapacityError
from runtime.worktrees import WorktreeService, git_run, resolve_commit


def request(path, *, method="GET", payload=None, service=None, worktrees=None,
            owner="alice", role="admin", headers=None, authorized=True, disconnect=False):
    handler = object.__new__(RequestHandler)
    handler.path = path
    body = json.dumps(payload).encode() if payload is not None else b""
    handler.headers = {"Content-Type": "application/json", "Content-Length": str(len(body)),
                       "Host": "localhost:8000", **(headers or {})}
    handler.rfile = io.BytesIO(body)
    handler.auth_user = AuthenticatedUser(user_id=owner, role=role)
    handler._authorize = Mock(return_value=authorized)
    handler.agent_service = service or Mock()
    handler.worktree_service = worktrees
    responses = []
    events = []
    handler._send_json = lambda value, status=HTTPStatus.OK, headers=None: responses.append((status, value))
    handler._send_stream_headers = Mock()
    handler._send_stream_event = Mock(side_effect=BrokenPipeError) if disconnect else events.append
    getattr(handler, "do_" + method)()
    return responses, events, handler


def test_submit_uses_authenticated_identity_and_durable_admission():
    service = Mock()
    service.submit.return_value = {"run_id": "run_owned", "status": "queued"}
    with patch("web.server._validated_model_profile", return_value=""):
        response, _, _ = request("/api/chat/runs", method="POST", service=service,
                                payload={"message": " work ", "request_id": "id1", "session_id": "chat1"})
    assert response == [(202, {"run": service.submit.return_value})]
    args = service.submit.call_args.kwargs
    assert (args["user_id"], args["session_id"], args["content"], args["request_id"]) == ("alice", "chat1", "work", "id1")
    assert args["coding_task"] == {}  # Existing task binding must remain intact.


@pytest.mark.parametrize("extra", [{"run_id": "chosen"}, {"user_id": "other"},
    {"session_id": "web:other:chat"}, {"coding_task": {"run_id": "chosen"}},
    {"coding_task": []}, {"thinking_enabled": "false"}])
def test_client_cannot_forge_run_identity_or_ambiguous_contract(extra):
    service = Mock()
    with patch("web.server._validated_model_profile", return_value=""):
        response, _, _ = request("/api/chat/runs", method="POST", service=service,
                                payload={"message": "work", "request_id": "id1", **extra})
    assert response[0][0] == 400
    service.submit.assert_not_called()


@pytest.mark.parametrize("error,status", [(RunConflictError("busy"), 409),
    (RunCapacityError("capacity"), 429), (RunNotFoundError("not found"), 404)])
def test_admission_error_statuses(error, status):
    service = Mock()
    service.submit.side_effect = error
    with patch("web.server._validated_model_profile", return_value=""):
        response, _, _ = request("/api/chat/runs", method="POST", service=service,
                                payload={"message": "work", "request_id": "id1"})
    assert response[0][0] == status


def test_event_projection_preserves_outer_identity_and_sequence():
    service = Mock()
    run = {"run_id": "outer", "session_id": "chat1", "status": "completed", "last_seq": 4}
    service.get_run.return_value = run
    service.events_after.return_value = [
        {"type": "event", "run_id": "outer", "session_id": "chat1", "seq": 2,
         "data": {"event": "tool.call.started", "run_id": "child", "payload": {"tool_name": "bash", "arguments_preview": "pytest"}}},
        {"type": "assistant_completed", "run_id": "outer", "session_id": "chat1", "seq": 3, "data": {"has_content": True}},
        {"type": "complete", "run_id": "outer", "session_id": "chat1", "seq": 4, "reply": "done"},
    ]
    with patch("web.server.read_session", return_value={"id": "owned"}) as read:
        responses, events, _ = request("/api/chat/runs/outer/events?after=1", service=service)
    assert not responses
    assert [event["seq"] for event in events] == [2, 3, 4]
    assert events[0]["run_id"] == "outer"
    assert events[0]["source_run_id"] == "child"
    assert events[0]["tool"] == "bash"
    assert events[1]["has_content"] is True
    assert events[2]["session"] == {"id": "owned"}
    read.assert_called_once_with("chat1", user_id="alice", message_limit=40)
    service.events_after.assert_called_once_with("outer", user_id="alice", after=1, wait_seconds=10)


def test_subscriber_disconnect_does_not_cancel_background_work():
    service = Mock()
    service.get_run.return_value = {"run_id": "outer", "session_id": "chat1", "status": "running", "last_seq": 1}
    service.events_after.return_value = [{"type": "delta", "seq": 1, "text": "a"}]
    request("/api/chat/runs/outer/events", service=service, disconnect=True)
    service.cancel_run.assert_not_called()
    service.request_cancel.assert_not_called()


def test_missing_or_foreign_run_returns_404_before_stream_headers():
    service = Mock()
    service.get_run.side_effect = RunNotFoundError("Run not found")
    responses, _, handler = request("/api/chat/runs/foreign/events", service=service)
    assert responses[0][0] == 404
    handler._send_stream_headers.assert_not_called()
    service.events_after.assert_not_called()


@pytest.mark.parametrize("headers,status", [({"Origin": "https://other.invalid"}, 403),
    ({"Content-Type": "text/plain"}, 415), ({"Content-Length": "-1"}, 400),
    ({"Sec-Fetch-Site": "cross-site"}, 403)])
def test_mutation_requires_same_origin_json_and_bounded_body(headers, status):
    service = Mock()
    responses, _, _ = request("/api/chat/runs/outer/cancel", method="POST", payload={}, service=service, headers=headers)
    assert responses[0][0] == status
    service.cancel_run.assert_not_called()


def test_stop_targets_exact_owned_run():
    service = Mock()
    service.cancel_run.return_value = {"run_id": "outer", "status": "cancelling"}
    responses, _, _ = request("/api/chat/runs/outer/cancel", method="POST", payload={}, service=service)
    assert responses[0][0] == 200
    service.cancel_run.assert_called_once_with("outer", user_id="alice")


def test_coding_routes_enforce_auth_and_admin_before_storage():
    worktrees = Mock()
    for path in ("/api/coding/worktrees", "/api/coding/deliveries"):
        assert request(path, authorized=False, worktrees=worktrees)[0] == []
        assert request(path, role="user", worktrees=worktrees)[0][0][0] == 403
    worktrees.list.assert_not_called()


def test_http_worktree_delivery_round_trip_and_owner_boundary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git_run(repo, ["init", "-b", "main"])
    (repo / "value.txt").write_text("base\n")
    git_run(repo, ["add", "."])
    git_run(repo, ["-c", "user.name=Fixture", "-c", "user.email=fixture@localhost", "commit", "-m", "base"])
    original = resolve_commit(repo)
    worktrees = WorktreeService(tmp_path / "managed")
    binding = worktrees.create(repo, task_id="task1", owner_id="alice")
    wid = binding["worktree_id"]
    (Path(binding["workspace_root"]) / "value.txt").write_text("task change\n")
    assert request(f"/api/coding/worktrees/{wid}", worktrees=worktrees, owner="bob")[0][0][0] == 404
    assert request("/api/coding/worktrees", worktrees=worktrees, owner="bob")[0][0][1] == {"worktrees": []}
    response = request(f"/api/coding/worktrees/{wid}/checkpoint", method="POST", payload={}, worktrees=worktrees)[0][0]
    assert response[0] == 200
    prepared = request("/api/coding/deliveries", method="POST", payload={"worktree_id": wid}, worktrees=worktrees)[0][0]
    assert prepared[0] == 200
    did = prepared[1]["delivery"]["id"]
    assert request(f"/api/coding/deliveries/{did}", owner="bob", worktrees=worktrees)[0][0][0] == 404
    response = request(f"/api/coding/deliveries/{did}/verify", method="POST", payload={"command": "test -f value.txt"}, worktrees=worktrees)[0][0]
    assert response[0] == 200 and response[1]["delivery"]["status"] == "verified"
    rejected = request(f"/api/coding/deliveries/{did}/publish", method="POST", payload={"target_ref": "refs/heads/main"}, worktrees=worktrees)[0][0]
    assert rejected[0] == 400
    published = request(f"/api/coding/deliveries/{did}/publish", method="POST", payload={}, worktrees=worktrees)[0][0]
    assert published[0] == 200 and published[1]["delivery"]["status"] == "published"
    assert resolve_commit(repo) == original
    assert (repo / "value.txt").read_text() == "base\n"


def test_coding_resource_contention_returns_conflict_instead_of_dropped_connection():
    from runtime.workspace_lease import WorkspaceBusyError
    worktrees = Mock()
    worktrees.list.return_value = [{"worktree_id": "wt_owned"}]
    with patch("web.coding_workspaces.WorktreeDeliveryService") as service:
        service.return_value.checkpoint.side_effect = WorkspaceBusyError("busy")
        responses, _, _ = request("/api/coding/worktrees/wt_owned/checkpoint", method="POST", payload={}, worktrees=worktrees)
    assert responses == [(409, {"error": "busy"})]
