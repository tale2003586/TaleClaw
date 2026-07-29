from __future__ import annotations

import pytest

from runtime.context.artifacts import ArtifactStore
from runtime.context.long_content import LongContentDetector


def test_token_limit_is_primary_and_externalizes_to_short_reference(tmp_path) -> None:
    store = ArtifactStore(tmp_path)
    detector = LongContentDetector(
        store,
        max_tokens=5,
        max_chars=10_000,
        max_bytes=10_000,
        token_estimator=lambda text: 6 if "payload" in text else 1,
    )

    result = detector.externalize("Please inspect this payload.\n\npayload" * 20, name="request.txt")

    assert result.externalized
    assert result.assessment.reasons == ("token_limit",)
    assert result.artifact_ref is not None
    assert len(result.content) < 500
    assert result.artifact_ref.artifact_id in result.content
    assert store.read_artifact(result.artifact_ref).startswith("Please inspect")


def test_character_and_byte_guards_cover_large_json_and_logs(tmp_path) -> None:
    detector = LongContentDetector(
        ArtifactStore(tmp_path),
        max_tokens=1_000_000,
        max_chars=10,
        max_bytes=20,
        token_estimator=lambda text: 1,
    )

    json_result = detector.externalize({"records": ["x" * 20]}, artifact_type="json", name="response.json")
    log_result = detector.externalize("log line\n" + "z" * 30, artifact_type="tool_result")

    assert json_result.assessment.reasons == ("character_guard", "byte_guard")
    assert json_result.content.startswith("The supplied structured content")
    assert json_result.artifact_ref is not None
    assert log_result.assessment.is_long
    assert log_result.artifact_ref is not None


def test_short_content_is_preserved_without_artifact_write(tmp_path) -> None:
    detector = LongContentDetector(
        ArtifactStore(tmp_path),
        max_tokens=100,
        max_chars=100,
        max_bytes=100,
        token_estimator=lambda text: 1,
    )

    result = detector.externalize_tool_result("ok", name="status")

    assert not result.externalized
    assert result.content == "ok"
    assert result.artifact_ref is None


def test_artifact_write_failure_does_not_publish_a_replacement() -> None:
    class FailingArtifactStore:
        def put_artifact(self, *_args, **_kwargs):
            raise OSError("artifact storage unavailable")

    detector = LongContentDetector(
        FailingArtifactStore(),
        max_tokens=1,
        max_chars=10,
        max_bytes=10,
    )
    original = "Keep this instruction.\n\n" + ("large payload " * 100)

    with pytest.raises(OSError, match="artifact storage unavailable"):
        detector.externalize(original)

    assert "artifact://" not in original
