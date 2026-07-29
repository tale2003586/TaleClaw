from __future__ import annotations

import hashlib
import json

import pytest

from runtime.context.artifacts import ArtifactNotFoundError, ArtifactStore


def test_put_is_content_addressed_and_survives_store_restart(tmp_path) -> None:
    store = ArtifactStore(tmp_path / "artifacts")
    first = store.put_artifact(
        "alpha\nbeta\ngamma",
        artifact_type="log",
        name="build.log",
        metadata={"source": "tool"},
    )
    second = store.put_artifact("alpha\nbeta\ngamma", artifact_type="text", name="ignored.txt")

    assert first == second
    assert first.content_hash == hashlib.sha256(b"alpha\nbeta\ngamma").hexdigest()
    assert store.get_artifact_metadata(first).metadata == {"source": "tool"}

    restarted = ArtifactStore(tmp_path / "artifacts")
    metadata = restarted.get_artifact_metadata(first.storage_uri)
    assert metadata.name == "build.log"
    assert metadata.size_bytes == len(b"alpha\nbeta\ngamma")
    assert metadata.size_chars == len("alpha\nbeta\ngamma")
    assert restarted.read_artifact(first) == "alpha\nbeta\ngamma"


def test_artifact_reads_searches_outlines_and_samples_text(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    text = "# Build\nintro\n## Tests\nneedle result\nlast line"
    ref = store.put_artifact(text, artifact_type="tool_result", name="report.md")

    assert store.read_artifact_range(ref, 2, 7) == "Build"
    assert store.read_artifact_range(ref, 0, 5, as_bytes=True) == b"# Bui"
    matches = store.search_artifact(ref, "NEEDLE")
    assert matches[0]["line"] == 4
    assert matches[0]["line_text"] == "needle result"
    assert store.get_artifact_outline(ref) == [
        {"line": 1, "level": 1, "title": "Build"},
        {"line": 3, "level": 2, "title": "Tests"},
    ]
    sampled = store.sample_artifact(ref, max_chars=40)
    assert len(sampled) <= 40
    assert "truncated" in sampled


def test_json_outline_and_metadata_are_not_content_copies(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_artifact('{"result": [1, 2], "status": "ok"}', artifact_type="json")

    assert store.get_artifact_outline(ref) == [
        {"line": None, "level": 1, "title": "result", "kind": "list"},
        {"line": None, "level": 1, "title": "status", "kind": "str"},
    ]
    metadata_path = tmp_path / "metadata" / f"{ref.artifact_id}.json"
    persisted = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "result" not in persisted["metadata"]
    assert "content" not in persisted


def test_missing_and_invalid_ranges_are_rejected(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    ref = store.put_artifact("abc")

    with pytest.raises(ValueError):
        store.read_artifact_range(ref, 2, 1)
    with pytest.raises(ArtifactNotFoundError):
        store.get_artifact_metadata("artifact://not-an-artifact")
