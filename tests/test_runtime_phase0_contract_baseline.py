from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

from agents.definitions import BOT_AGENT_SPEC
from agents.definitions import CODING_AGENT_SPEC
from runtime.context import ContextBuilder, PromptAssetsService
from runtime.context.budget import ContextBudgeter
from runtime.context.providers import DEFAULT_CONTEXT_PROVIDERS
from applications.coding.context_state import build_coding_context_view
from runtime.token_estimator import estimate_tokens
from runtime.sessions import Session
from tests.fakes.fake_tools import RecordingTool
from tools.schema import function_tool
from tools.tool_registry import ToolRegistry
from tools.tool_registry import build_lead_tool_registry


SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


def _normalized_context(context):
    report = context.report.to_dict()
    return {
        "roles": [message["role"] for message in context.messages],
        "section_order": list(report["sections"]),
        "section_rendered": {
            name: bool(section["rendered_chars"])
            for name, section in report["sections"].items()
        },
        "estimated_tokens": estimate_tokens(context.messages),
    }


def _snapshot(name):
    return json.loads((SNAPSHOT_DIR / name).read_text(encoding="utf-8"))


def test_chat_and_coding_context_contract_snapshot(tmp_path):
    (tmp_path / ".agent").mkdir()
    (tmp_path / ".agent" / "assistant.md").write_text("assistant rules", encoding="utf-8")
    (tmp_path / ".agent" / "coding.md").write_text("coding rules", encoding="utf-8")
    (tmp_path / "AGENTS.md").write_text("project rules", encoding="utf-8")

    chat = Session(id="chat:snapshot")
    chat.add_message("user", "hello")
    coding = Session(
        id="task:snapshot",
        active_agent="coding",
        metadata={"kind": "coding_application", "workspace_root": str(tmp_path)},
    )
    coding.add_message("user", "inspect the repository")
    budgeter = ContextBudgeter.from_env()
    builder = ContextBuilder(
        budgeter=budgeter,
        context_providers=DEFAULT_CONTEXT_PROVIDERS,
        prompt_assets_service=PromptAssetsService(
            budgeter=budgeter,
            instruction_root=tmp_path,
            skill_loader=SimpleNamespace(catalog_text=lambda: ""),
        ),
        coding_context_view_builder=build_coding_context_view,
    )

    actual = {
        "chat": _normalized_context(builder.build(session=chat, profile=BOT_AGENT_SPEC)),
        "coding": _normalized_context(builder.build(session=coding, profile=CODING_AGENT_SPEC)),
    }

    expected = _snapshot("runtime_phase0_context.json")
    # Token counts are recorded as a baseline but allowed a narrow estimator-version margin.
    for context_name in ("chat", "coding"):
        assert abs(
            actual[context_name]["estimated_tokens"]
            - expected[context_name]["estimated_tokens"]
        ) <= 2
        actual[context_name]["estimated_tokens"] = expected[context_name]["estimated_tokens"]
    assert actual == expected


def test_tool_schema_and_visibility_contract_snapshot():
    registry = ToolRegistry()
    handler = RecordingTool()
    definitions = [
        ("chat_read", {"bot"}, False, True),
        ("coding_write", {"coding"}, True, True),
        ("deferred_coding", {"coding"}, False, False),
    ]
    for name, modes, admin_only, always_on in definitions:
        registry.register(
            function_tool(
                name,
                f"{name} description",
                {"value": {"type": "integer"}},
                ["value"],
            ),
            handler,
            allowed_agents=modes,
            admin_only=admin_only,
            always_on=always_on,
        )

    chat = Session(id="chat", active_agent="bot", metadata={"user_role": "user"})
    admin = Session(id="coding", active_agent="coding", metadata={"user_role": "admin"})
    user = Session(id="coding-user", active_agent="coding", metadata={"user_role": "user"})
    actual = {
        "catalog": registry.catalog(),
        "visible": {
            "chat_user": sorted(registry.visible_names_for_turn(chat, "bot")),
            "coding_admin": sorted(registry.visible_names_for_turn(admin, "coding")),
            "coding_user": sorted(registry.visible_names_for_turn(user, "coding")),
        },
        "authorization": {
            "chat_cannot_write": registry.execution_error_for_turn(
                "coding_write", session=chat, mode="bot"
            ),
            "user_cannot_admin_write": registry.execution_error_for_turn(
                "coding_write", session=user, mode="coding"
            ),
            "deferred_requires_unlock": registry.execution_error_for_turn(
                "deferred_coding", session=admin, mode="coding"
            ),
        },
    }

    assert actual == _snapshot("runtime_phase0_tools.json")


def test_real_lead_tool_contract_snapshot():
    registry = build_lead_tool_registry()
    chat = Session(id="lead:chat", metadata={"user_role": "admin"})
    coding = Session(id="lead:coding", active_agent="coding", metadata={"user_role": "admin"})

    def schema_hash(name):
        payload = json.dumps(
            registry._tools[name].schema,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    actual = {
        "registered_count": len(registry.catalog()),
        "chat_visible": sorted(registry.visible_names_for_turn(chat, "bot")),
        "coding_visible": sorted(registry.visible_names_for_turn(coding, "coding")),
        "critical_schema_sha256": {
            name: schema_hash(name)
            for name in (
                "bash",
                "edit_file",
                "parallel_tasks",
                "read_file",
                "task",
                "tool_search",
                "write_file",
            )
        },
    }

    assert actual == _snapshot("runtime_phase0_lead_tools.json")
