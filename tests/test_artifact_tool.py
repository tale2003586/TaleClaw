from __future__ import annotations

import json

import pytest

from runtime.context import ArtifactStore
from runtime.sessions import Session
from tools.handlers import run_read_artifact
from tools.tool_registry import build_lead_tool_registry, build_teammate_tool_registry


def test_read_artifact_pages_externalized_text(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("0123456789" * 80, artifact_type="user_input")

    first = run_read_artifact(ref.storage_uri, limit=200, artifact_store=store)
    header_text, content = first.split("\n\n", 1)
    header = json.loads(header_text)

    assert content == ("0123456789" * 20)
    assert header["artifact_ref"] == ref.storage_uri
    assert header["truncated"] is True
    assert header["next_offset"] == 200

    second = run_read_artifact(
        ref.artifact_id,
        offset=header["next_offset"],
        limit=200,
        artifact_store=store,
    )
    _, second_content = second.split("\n\n", 1)
    assert second_content == "0123456789" * 20


def test_read_artifact_can_search_large_user_input(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact(
        "prefix\nTARGET one\nmiddle\nTARGET two\nsuffix",
        artifact_type="user_input",
    )

    result = json.loads(
        run_read_artifact(
            ref.storage_uri,
            query="TARGET",
            artifact_store=store,
        )
    )

    assert result["match_count"] == 2
    assert [match["line"] for match in result["matches"]] == [2, 4]


def test_read_artifact_search_does_not_return_an_unbounded_single_line(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    ref = store.put_artifact("x" * 100_000 + "TARGET" + "y" * 100_000)

    output = run_read_artifact(ref.storage_uri, query="TARGET", artifact_store=store)
    result = json.loads(output)

    assert len(output) < 2_000
    assert "line_text" not in result["matches"][0]


def test_read_artifact_reports_unknown_reference(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")

    with pytest.raises(ValueError, match="Artifact not found"):
        run_read_artifact("artifact://art_missing", artifact_store=store)


def test_read_artifact_is_deferred_and_can_be_unlocked_for_all_agent_modes(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    lead = build_lead_tool_registry(artifact_store=store)
    teammate = build_teammate_tool_registry("reader", artifact_store=store)

    for mode, registry in (
        ("bot", lead),
        ("coding", lead),
        ("teammate", teammate),
    ):
        session = Session(id=f"test:{mode}", active_agent=mode)
        assert "read_artifact" not in registry.visible_names_for_turn(session, mode)
        registry.execute(
            "tool_search",
            {"query": "read an externalized artifact"},
            session=session,
            mode=mode,
        )
        assert "read_artifact" in registry.visible_names_for_turn(session, mode)

    ref = store.put_artifact("full request", artifact_type="user_input")
    session = Session(id="test:call", active_agent="coding")
    lead.execute(
        "tool_search",
        {"query": "read an externalized artifact"},
        session=session,
        mode="coding",
    )
    output = lead.execute(
        "read_artifact",
        {"artifact_ref": ref.storage_uri},
        session=session,
        mode="coding",
    )
    assert "full request" in output
