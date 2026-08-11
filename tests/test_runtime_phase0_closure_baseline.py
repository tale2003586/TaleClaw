from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from applications.coding.runner import CodingApplication
from applications.coding.session import TaskSessionFactory
from gateway.feishu.adapter import FeishuGateway
from gateway.telegram.adapter import TelegramGateway
from tests.fakes import make_agent_spec
from runtime.context import ContextBuilder, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.runtime import Runtime
from runtime.trace.run_state import RunState
from runtime.trace.trace_store import TraceStore
from runtime.workspace import WorkspaceResolver
from runtime.sessions import Session
from tests.fakes.fake_tools import RecordingTool, registry_with_tool
from tests.fakes.in_memory_sessions import InMemorySessionManager
from tests.fakes.scripted_model import FinalResponse, ScriptedModel
from tools.executor import ToolExecutor


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"
CODING_AGENT_SPEC = make_agent_spec("coding", "offline coding prompt", "coding")


class FailingClient:
    async def send_message(self, *args, **kwargs):
        raise TimeoutError("deterministic delivery timeout")


class InMemoryOutbox:
    def __init__(self):
        self.item = {
            "id": 7,
            "chat_id": "123",
            "text": "deliver me",
            "message_type": "text",
            "attempts": 0,
            "status": "pending",
            "last_error": "",
        }

    def list_pending_messages(self, *, limit):
        return [dict(self.item)] if self.item["status"] == "pending" else []

    def mark_message_sent(self, item_id):
        self.item["status"] = "sent"

    def mark_message_failed(self, item_id, *, error, max_attempts):
        self.item["attempts"] += 1
        self.item["last_error"] = error
        if self.item["attempts"] >= max_attempts:
            self.item["status"] = "failed"


def _runtime(tmp_path: Path, provider: ScriptedModel) -> Runtime:
    budgeter = ContextBudgeter.from_env()
    return Runtime(
        tools=registry_with_tool(
            "read_file",
            RecordingTool(output="unused"),
            modes={"coding"},
        ),
        provider=provider,
        model="fake-model",
        tool_executor=ToolExecutor([]),
        context_builder=ContextBuilder(
            budgeter=budgeter,
            prompt_assets_service=PromptAssetsService(
                budgeter=budgeter,
                instruction_root=tmp_path,
                skill_loader=SimpleNamespace(catalog_text=lambda: ""),
            ),
        ),
        max_tokens=256,
        max_reasoning_steps=4,
    )


def test_full_coding_task_lifecycle_is_offline_and_persists_all_artifacts(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("TRACE_INDEX_ENABLED", "0")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "before.txt").write_text("stable\n", encoding="utf-8")
    provider = ScriptedModel([
        FinalResponse("Task completed without changing the workspace."),
        FinalResponse(json.dumps({
            "summary": "Validated the coding lifecycle.",
            "conclusions": [{
                "category": "project",
                "content": "Phase 0 coding lifecycle uses isolated task sessions.",
                "evidence": "task artifacts",
                "confidence": 0.9,
            }],
        })),
    ])
    sessions = InMemorySessionManager()
    runtime = _runtime(tmp_path, provider)
    runner = CodingApplication(
        sessions=sessions,
        base_runtime=runtime,
        workspace_resolver=WorkspaceResolver(
            allowed_roots=[tmp_path],
            default_workspace=workspace,
        ),
    )
    runner.factory = TaskSessionFactory(sessions, root=tmp_path / "task-sessions")
    parent = Session(
        id="web:phase0-parent",
        active_agent="coding",
        metadata={"user_id": "phase0", "user_role": "admin"},
    )
    parent.add_message("user", "prior parent turn")
    run_state = RunState.create(
        run_id="phase0-full-coding",
        session_id=parent.id,
        mode="coding",
    )
    trace = TraceStore(tmp_path / "runs")
    trace.start_run(run_state)

    reply = runner.run_coding_task(
        parent_session=parent,
        user_text="validate the complete lifecycle",
        agent_spec=CODING_AGENT_SPEC,
        workspace_root=workspace,
        run_state=run_state,
        trace_store=trace,
    )
    run_state.finish_success(reply)
    trace.write_run_state(run_state)
    trace.write_report(run_state, {"phase": 0})

    task = run_state.metadata["coding_application"]
    coding_application = sessions.get_or_create(task["coding_application_id"])
    task_root = runner.factory.root / task["task_id"]
    assert coding_application.metadata["status"] == "completed"
    assert coding_application.id in sessions.saved_ids
    assert (task_root / "TASK_LOG.md").exists()
    conclusions = json.loads((task_root / "CONCLUSIONS.json").read_text(encoding="utf-8"))
    assert conclusions["llm_candidates"][0]["content"].startswith(
        "Phase 0 coding lifecycle"
    )
    assert conclusions["promoted"] == []
    assert not (task_root / "memory").exists()
    diff = json.loads(
        (trace.run_dir(run_state) / "workspace_diff.json").read_text(encoding="utf-8")
    )
    assert {
        key: diff["summary"][key]
        for key in ("created", "modified", "deleted")
    } == {"created": 0, "modified": 0, "deleted": 0}
    events = [
        json.loads(line)["event"]
        for line in (trace.run_dir(run_state) / "trace.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    expected = json.loads(
        (SNAPSHOT_DIR / "runtime_phase0_coding_lifecycle_events.json").read_text(
            encoding="utf-8"
        )
    )
    assert events == expected
    assert "TaskSession" in reply


@pytest.mark.parametrize(
    ("gateway_type", "channel"),
    [(TelegramGateway, "telegram"), (FeishuGateway, "feishu")],
)
def test_gateway_delivery_failure_has_stable_persisted_state(
    gateway_type,
    channel,
):
    store = InMemoryOutbox()
    gateway = object.__new__(gateway_type)
    gateway.store = store
    gateway.client = FailingClient()
    gateway.outbox_limit = 10
    gateway.outbox_max_attempts = 2

    asyncio.run(gateway.flush_outbox())
    first = dict(store.item)
    asyncio.run(gateway.flush_outbox())
    final = dict(store.item)
    actual = {
        "channel": channel,
        "first_failure": {
            "attempts": first["attempts"],
            "status": first["status"],
            "last_error": first["last_error"],
        },
        "terminal_failure": {
            "attempts": final["attempts"],
            "status": final["status"],
            "last_error": final["last_error"],
        },
    }
    snapshots = json.loads(
        (SNAPSHOT_DIR / "runtime_phase0_gateway_delivery_failures.json").read_text(
            encoding="utf-8"
        )
    )
    assert actual == snapshots[channel]
