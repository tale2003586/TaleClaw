"""Usable context-policy configuration through the native coding-task transport."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from applications.coding.task_lifecycle import coding_task_options
from tests.test_task_continuation_regressions import SPEC, active, start


def test_task_policy_options_validate_and_copy_config():
    settings = {"name": "adaptive", "recent_groups": 3, "preview_chars": 64}
    result = coding_task_options({"mode": "continue", "context_policy": settings})
    assert result["context_policy"] == settings
    settings["preview_chars"] = 400
    assert result["context_policy"]["preview_chars"] == 64
    assert coding_task_options({"context_policy": "masking"})["context_policy"] == "masking"


@pytest.mark.parametrize("policy", ["typo", [], True, {"name": "raw", "typo": 1},
                                   {"soft_ratio": "large"}, {"recent_groups": False},
                                   {"name": "native", "native_fallback": "silent"}])
def test_invalid_policy_rejected_before_any_task_or_provider_mutation(tmp_path, policy):
    runner, sessions, parent, task = start(tmp_path)
    before = deepcopy(task)
    calls = len(runner.base_runtime.agent_runner.provider.calls)
    session_ids = set(sessions.sessions)
    with pytest.raises(ValueError, match="context_policy"):
        runner.run_coding_task(parent_session=parent, user_text="follow up", agent_spec=SPEC,
                               task_mode="branch", context_policy=policy)
    assert set(sessions.sessions) == session_ids
    assert len(runner.base_runtime.agent_runner.provider.calls) == calls
    assert task.messages == before.messages and task.metadata == before.metadata


def test_python_api_switches_policy_on_new_continue_and_branch(tmp_path):
    runner, sessions, parent, original = start(tmp_path)
    parent.metadata["context_policy"] = "masking"
    runner.run_coding_task(parent_session=parent, user_text="new parser", agent_spec=SPEC,
                           task_mode="new", context_policy="raw")
    task = active(sessions, parent)
    assert task.id != original.id
    assert task.metadata["context_policy"] == "raw"
    config = {"name": "adaptive", "preview_chars": 72}
    runner.run_coding_task(parent_session=parent, user_text="inspect edge case", agent_spec=SPEC,
                           task_mode="continue", context_policy=config)
    assert active(sessions, parent) is task
    assert task.metadata["context_policy"] == config
    config["preview_chars"] = 444
    assert task.metadata["context_policy"]["preview_chars"] == 72
    assert task.metadata["context_policy_audit"]["policy"] == "adaptive"
    runner.run_coding_task(parent_session=parent, user_text="alternative", agent_spec=SPEC,
                           task_mode="branch", context_policy="masking")
    branch = active(sessions, parent)
    assert branch.id != task.id
    assert branch.metadata["context_policy"] == "masking"
    assert task.metadata["context_policy"]["name"] == "adaptive"
    assert parent.metadata["context_policy"] == "masking"


def test_explicit_task_override_survives_followup_parent_default_and_branch(tmp_path):
    runner, sessions, parent, task = start(tmp_path)
    parent.metadata["context_policy"] = "masking"
    runner.run_coding_task(parent_session=parent, user_text="follow up", agent_spec=SPEC)
    assert task.metadata["context_policy"] == "masking"
    runner.run_coding_task(parent_session=parent, user_text="try adaptive", agent_spec=SPEC,
                           context_policy="adaptive")
    parent.metadata["context_policy"] = "raw"
    runner.run_coding_task(parent_session=parent, user_text="next step", agent_spec=SPEC)
    assert task.metadata["context_policy"] == "adaptive"
    runner.run_coding_task(parent_session=parent, user_text="branch", agent_spec=SPEC, task_mode="branch")
    assert active(sessions, parent).metadata["context_policy"] == "adaptive"


def test_web_chat_forwards_valid_policy_and_returns_400_for_invalid_policy(monkeypatch):
    from web.server import RequestHandler
    handler = object.__new__(RequestHandler)
    handler.headers = {"Content-Type": "application/json", "Host": "localhost:8000"}
    settings = {"mode": "continue", "context_policy": {"name": "adaptive", "preview_chars": 64}}
    handler._read_json_body = lambda: {"message": "inspect", "coding_task": settings}
    handler._current_user = lambda: SimpleNamespace(user_id="alice", role="admin")
    calls, replies = [], []
    run = {"run_id": "run_selector", "session_id": "default", "status": "completed"}
    handler.agent_service = SimpleNamespace(
        submit=lambda **kwargs: calls.append(kwargs) or run,
        get_run=lambda *_args, **_kwargs: run,
        events_after=lambda *_args, **_kwargs: [{"type": "complete", "seq": 1, "reply": "done"}],
    )
    handler._send_json = lambda payload, **kwargs: replies.append((payload, kwargs))
    monkeypatch.setattr("web.server.read_session", lambda *_args, **_kwargs: {})
    handler._handle_chat()
    assert calls[0]["coding_task"]["context_policy"] == settings["context_policy"]
    settings["context_policy"] = {"name": "adaptive", "soft_ratio": "invalid"}
    handler._handle_chat()
    assert len(calls) == 1
    assert replies[-1][1]["status"] == 400
    assert "context_policy" in replies[-1][0]["error"]


def test_turn_coordinator_passes_context_policy_to_coding_runner():
    from applications.turn_coordinator import TurnCoordinator
    from runtime.sessions.session import Session
    calls = []
    coordinator = TurnCoordinator(None, None, None, None, coding_application=SimpleNamespace(
        run_coding_task=lambda **kwargs: calls.append(kwargs) or "done"))
    selector = {"mode": "continue", "context_policy": "adaptive"}
    coordinator._execute(Session("parent"), SimpleNamespace(content="inspect", metadata={"coding_task": selector}),
                         SimpleNamespace(agent_spec=SPEC), SimpleNamespace(run_id="r"), None)
    assert calls[0]["context_policy"] == "adaptive"
