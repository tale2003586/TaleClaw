"""Skill edits must take effect behind the real registry's existing tool unlocks."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

import skill_runtime
from runtime.agent_spec import AgentSpec, ContextPolicy, ToolSet
from runtime.sessions import Session
from skill_runtime import SkillLoader
from tools.spec import ToolExposure, ToolSpec
from tools.tool_registry import build_lead_tool_registry


_SKILL_ID = "review-directory"
_QUERY = "hotreload review workflow"
_OLD_BODY = "BODY_VERSION_ONE"
_NEW_BODY = "BODY_VERSION_TWO"


def _document(*, name="review", mode="coding", body=_OLD_BODY, extra=""):
    return (
        f"---\nname: {name}\ndescription: {_QUERY}\n"
        f"triggers: [hotreload]\napplies_to: [{mode}]\n"
        f"{extra}---\n{body}\n"
    )


@pytest.fixture
def skill_runtime_case(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    path = root / _SKILL_ID / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(_document(), encoding="utf-8")
    loader = SkillLoader(root)
    monkeypatch.setattr(skill_runtime, "SKILL_LOADER", loader)
    registry = build_lead_tool_registry(
        team=SimpleNamespace(), include_subagent_tools=False,
    )
    return SimpleNamespace(
        path=path,
        loader=loader,
        registry=registry,
        session=Session("hot-reload", metadata={"user_role": "user"}),
        spec=AgentSpec(name="reviewer", tool_set=ToolSet(mode="coding"), skills=("review",)),
    )


def _execute(case, name, arguments, *, spec=None):
    spec = spec or case.spec
    return case.registry.execute(
        name, arguments, session=case.session,
        mode=spec.tool_set.mode, agent_spec=spec,
    )


def _search(case, *, spec=None):
    return _execute(case, "tool_search", {"query": _QUERY}, spec=spec)


def _load(case, name="review", *, spec=None):
    return _execute(case, "load_skill", {"name": name}, spec=spec)


def _update(case, **changes):
    current = case.loader.get_document(_SKILL_ID)
    return case.loader.update_document(_SKILL_ID, current["revision"], **changes)


def _unlock_and_load(case):
    assert "skill:review" in _search(case)
    assert "load_skill" in case.registry.visible_names_for_turn(
        case.session, "coding", agent_spec=case.spec,
    )
    assert _OLD_BODY in _load(case)


def _assert_not_loaded(result, *bodies):
    assert result.status in {"error", "denied"}
    assert "<skill " not in result
    for body in bodies:
        assert body not in result


def test_disable_and_reenable_affect_an_already_unlocked_registry(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)

    _update(case, enabled=False)
    # The tool stays unlocked; the loader must recheck the skill itself.
    assert "load_skill" in case.registry.visible_names_for_turn(
        case.session, "coding", agent_spec=case.spec,
    )
    _assert_not_loaded(_load(case), _OLD_BODY)
    assert "skill:review" not in _search(case)

    _update(case, enabled=True)
    assert "skill:review" in _search(case)
    assert _OLD_BODY in _load(case)


def test_content_edit_is_visible_without_rebuilding_registry_or_session(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)

    _update(case, content=_document(body=_NEW_BODY))
    loaded = _load(case)
    assert _NEW_BODY in loaded
    assert _OLD_BODY not in loaded


def test_external_delete_revokes_content_after_discovery(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)

    case.path.unlink()
    _assert_not_loaded(_load(case), _OLD_BODY)
    assert "skill:review" not in _search(case)


def test_renaming_document_does_not_expand_agent_skill_scope(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)

    _update(case, content=_document(name="outside-scope", body=_NEW_BODY))
    assert case.loader.get_document(_SKILL_ID)["id"] == _SKILL_ID
    _assert_not_loaded(_load(case), _OLD_BODY)
    _assert_not_loaded(_load(case, "outside-scope"), _NEW_BODY)
    assert "skill:outside-scope" not in _search(case)

    allowed = replace(case.spec, skills=("outside-scope",))
    assert "skill:outside-scope" in _search(case, spec=allowed)
    assert _NEW_BODY in _load(case, "outside-scope", spec=allowed)


def test_mode_edit_is_rechecked_after_tool_unlock(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)

    _update(case, content=_document(mode="bot", body=_NEW_BODY))
    _assert_not_loaded(_load(case), _NEW_BODY, _OLD_BODY)
    assert "skill:review" not in _search(case)

    bot = replace(case.spec, tool_set=ToolSet(mode="bot"))
    assert "skill:review" in _search(case, spec=bot)
    assert _NEW_BODY in _load(case, spec=bot)


def test_edited_metadata_cannot_override_include_skills_policy(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)
    _update(case, content=_document(body=_NEW_BODY, extra="include_skills: true\n"))
    denied = replace(case.spec, context_policy=ContextPolicy(include_skills=False))

    _assert_not_loaded(_load(case, spec=denied), _NEW_BODY)
    assert "skill:review" not in _search(case, spec=denied)
    assert "load_skill" not in case.registry.visible_names_for_turn(
        case.session, "coding", agent_spec=denied,
    )


@pytest.mark.parametrize("restriction", ["allow", "deny", "admin"])
def test_requires_tools_and_safety_metadata_do_not_grant_tool_permissions(
    skill_runtime_case, restriction,
):
    case = skill_runtime_case
    _unlock_and_load(case)
    calls = []
    tool_name = "restricted_fixture_action"
    case.registry.register(ToolSpec(
        schema={"type": "function", "function": {
            "name": tool_name,
            "description": _QUERY,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }},
        handler=lambda: calls.append("executed") or "executed",
        exposure=ToolExposure.DEFERRED,
        admin_only=restriction == "admin",
    ))
    if restriction == "allow":
        case.spec = replace(case.spec, tool_set=ToolSet(
            mode="coding", allow=("tool_search", "load_skill"),
        ))
    elif restriction == "deny":
        case.spec = replace(case.spec, tool_set=ToolSet(mode="coding", deny=(tool_name,)))

    _update(case, content=_document(body=_NEW_BODY, extra=(
        f"requires_tools: [{tool_name}]\n"
        "safety:\n  risk: low\n  admin_only: false\n"
    )))
    assert "skill:review" in _search(case)
    assert _NEW_BODY in _load(case)
    assert tool_name not in case.registry.visible_names_for_turn(
        case.session, "coding", agent_spec=case.spec,
    )
    result = _execute(case, tool_name, {})
    assert result.status == "denied"
    assert not calls


def test_registry_observes_another_loader_editing_the_shared_directory(skill_runtime_case):
    case = skill_runtime_case
    _unlock_and_load(case)
    editor = SkillLoader(case.loader.skills_dir)

    def edit(**changes):
        document = editor.get_document(_SKILL_ID)
        editor.update_document(_SKILL_ID, document["revision"], **changes)

    edit(enabled=False)
    _assert_not_loaded(_load(case), _OLD_BODY)
    assert "skill:review" not in _search(case)

    edit(content=_document(body=_NEW_BODY, extra="enabled: false\n"))
    _assert_not_loaded(_load(case), _OLD_BODY, _NEW_BODY)
    edit(enabled=True)
    assert "skill:review" in _search(case)
    loaded = _load(case)
    assert _NEW_BODY in loaded
    assert _OLD_BODY not in loaded
